Última modificación: 2026-10-11 | Creado: 2026-10-11

# Plan: Fase 3 (fix + verificación)

La especificación completa vive en `docs/iniciativas/archivo/003-fase3-fix-verificacion/spec.md`
(aprobada con enmiendas) y las tareas en `docs/iniciativas/archivo/003-fase3-fix-verificacion/todo.md`.

- **Objetivo:** un hallazgo `fix_with_approval` **aprobado** produce un parche, se
  verifica en un sandbox con la imagen del CI **por digest** y, solo si pasa, se
  publica como sugerencia inline. Si no pasa, no deja rastro y escala con el log.
  El agente **sigue sin escribir** el repo.
- **Enmiendas de arranque:**
  - **E6 (seam de entrega):** el formato (```` ```suggestion ```` vs ```` ```diff ````)
    vive tras un `DeliveryFormatter`; R1 (¿Bitbucket renderiza `suggestion`?)
    deja de bloquear. Verificado que `twg` no expone ningún concepto de `suggestion`.
  - **E7 (verificación desacoplada):** separar la verificación de la ruta de la
    review es requisito de v1, no optimización (revisión de R2).
  - **Q1:** runner rootless **en esta máquina** (herramienta rootless y user
    namespaces verificados; el demonio actual no es rootless).
  - **Q4:** P6 pasa a ser **precondición del runner** (ejecuta código de PRs de
    terceros), no "ya no bloquea".
- **Cerrado (Bloque 1, base pura):** `F1`–`F6`. Contrato de fix, validación local
  del parche, `LlmFixer`, selección de candidatos, nodo `fix` con el seam de
  entrega, y render/dedupe. **320 tests** en verde; camino pausa → decisión → fix →
  sugerencia verificada cubierto con fakes (`tests/test_fix_e2e.py`).
- **Cerrado (Bloque 2, runner):** `F7`–`F10`. **Enmienda de contrato (2026-10-11):**
  el runner es **síncrono** (`POST /verify` bloqueante con `run_key` idempotente),
  no `POST /runs` + sondeo. El adaptador HTTP, el servicio `sandbox_runner`
  (FastAPI interno, sin `docker.sock`, con runtime/workspace inyectables), la
  validación de `{targets}`, el `ci_image_digest` y el compose/secretos están
  hechos. Ver `tests/test_http_sandbox.py`, `tests/test_sandbox_runner.py`,
  `tests/test_compose.py`.
- **Pendiente (Bloque 3 + precondiciones):** `F11`–`F13` (spike R1 real, smoke
  real, docs) y el desacople **fuerte** de E7 (cola/consumidor de fixes aparte):
  el adaptador síncrono **no** lo cumple, solo lo simplifica. Smoke real del runner
  bloqueado por **P6** (precondición de seguridad).
