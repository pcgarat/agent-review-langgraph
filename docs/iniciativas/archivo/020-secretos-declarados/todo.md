Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Contrato de secretos declarado en el modelo

Checklist accionable. El diseño está en [`spec.md`](spec.md) (con la enmienda del
2026-10-17); el orden, en [`plan.md`](plan.md). Spec aprobada el 2026-10-17.

## Bloque 1 — Declaración y paridad (checkpoint C1)

- [x] **S1** `PLACEHOLDER_VALUES` público
  - `src/agent_review/settings.py`: hace público el set de placeholders y borra
    `_INSECURE_DEFAULTS`; `_is_placeholder` pasa a leer de él.
  - `api/main.py`, `web/auth.py` y `sandbox_runner/main.py` importan de aquí y borran
    `_INSECURE_DECISION_TOKENS`, `_INSECURE_SECRETS`, `_INSECURE_TOKENS` y las dos
    comparaciones a pelo (`secret == "change-me"`).
  - Verifica: `tests/test_web.py`, `tests/test_api.py`, `tests/test_sandbox_runner.py`.

- [x] **S2** Descriptor `Secret` y `secrets_for`
  - `src/agent_review/settings.py`: `Secret` (dataclass congelada, §5.1), la tabla
    `_FEATURES` (§5.3) y `secrets_for(settings, process, feature_enabled=None)`.
  - Declarar los 13 campos con nombre de secreto y los dos valores (`sandbox_runner_url`,
    `teams_notices_key`). La declaración va en `Annotated[...]` sobre el campo.
  - Las cuatro funciones actuales **no se tocan** todavía.
  - Verifica: `tests/test_contracts.py` o un `tests/test_secrets.py` nuevo.

- [x] **S3** Test de paridad *(red de seguridad, antes de delegar)*
  - Recorre los 48 escenarios (proveedor VCS × sandbox × panel × teams × LLM) y compara
    `secrets_for(s, proceso)` con `insecure_secrets`, `panel_insecure_secrets`,
    `fix_worker_insecure_secrets` y `teams_bot_insecure_secrets`.
  - Aceptación: verde sobre el código de hoy, con las funciones antiguas intactas.
  - Verifica: `tests/test_secrets.py`.

## Bloque 2 — Delegación y limpieza (checkpoint C2)

- [x] **S4** Las cuatro funciones delegan en `secrets_for`
  - `is_missing` se queda como predicado público; `insecure_secrets`,
    `panel_insecure_secrets`, `fix_worker_insecure_secrets` y
    `teams_bot_insecure_secrets` conservan nombre y firma y pasan a delegar (D6).
  - La rama del registro de modelos (`unresolved_secrets`) se conserva dentro de
    `secrets_for`, con su test doble.
  - Aceptación: el test de paridad de S3 sigue verde **sin cambios**.
  - Verifica: `tests/test_secrets.py`, `tests/test_api.py`, `tests/test_fix_worker.py`,
    `tests/test_teams_bot_settings.py`.

- [x] **S5** Invariantes I1 e I3
  - I1: test sobre `model_fields` — todo campo con nombre de secreto (o con `change-me`
    de default) declara su `Secret`, con la única excepción nombrada de
    `sandbox_runner_url` (`expect="url"`). Un campo nuevo sin declarar da rojo.
  - I3: un `required_in` o una `feature` desconocidos lanzan al construir; ninguna
    `feature` declarada se queda fuera de `_FEATURES`.
  - Verifica: `tests/test_secrets.py`.

## Bloque 3 — Cierre (checkpoint C3)

- [x] **S6** Documentos vivos
  - `docs/arquitectura.md` §7: D22 pasa a resuelta (o se retira la fila) y se apunta a
    `secrets_for` como la única vía de lectura.
  - `docs/operacion.md` §3: el contrato se declara en `Settings`; nota de que el
    runner importa el mismo predicado.
  - `docs/desarrollo.md` §4: invariante nuevo (un secreto, una declaración) y cómo
    añadir un campo secreto.
  - `CONTEXT.md`: «descriptor de secreto» y «contrato derivado».
  - `docs/adr/0022-…md`: estado a `Aceptado` (con la enmienda del modelo de dos ejes).
  - `docs/iniciativas/README.md`: fila de 020 al estado final.

- [x] **S7** Verificación final
  - `make ci` y `make docs-check` en verde; los tests de `production` de los cinco
    puntos de arranque pasan **sin tocarlos** (criterio 5).
