Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Personas deterministas de wiring y configuración

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). Spec aprobada el 2026-10-17 (Q1–Q3 y Q5 resueltas). Solo la
**fase 1** (motor + gate) está planificada al detalle; la fase 2 es una tarea paraguas
que se abre cuando haya un repo con perfil que declare `wiring:`.

## Bloque 1 — Contrato y acceso (checkpoint C1)

- [ ] **W1** Esquema `WiringConfig` y `config/wiring.yaml`
  - Modelo Pydantic (`extra="forbid"`, como `ModelsConfig`) con `services`,
    `settings` y `models` (§5.2 de la spec); `WIRING_CONFIG` en `Settings`
    (`config/wiring.yaml`) y `.env.example`.
  - Aceptación: un contrato que referencia un servicio o fichero inexistente falla
    con un mensaje que lo nombra; el fichero versionado valida.
  - Verifica: `tests/test_wiring_config.py`.
  - Ficheros: `src/agent_review/wiring.py`, `config/wiring.yaml`, `settings.py`.

- [ ] **W2** `FileSource` y lectura de `Settings` por AST (W12)
  - Puerto `FileSource` con `read(path) -> str | None` e implementación
    `RepoFileSource` (árbol de trabajo). Extractores AST: campos, defaults y
    `Secret` del `metadata` de `settings.py`.
  - Aceptación: el AST ve los mismos campos que `Settings.model_fields` (test de
    equivalencia); un fichero ilegible devuelve `None`, no lanza.
  - Verifica: `tests/test_wiring_files.py`.
  - Ficheros: `src/agent_review/wiring.py`.

### Checkpoint 1 (tras W1–W2)

- [ ] `make ci` en verde; contrato y acceso cubiertos.

## Bloque 2 — Las tres comprobaciones (checkpoint C2)

- [ ] **W3** (a) servicios de compose
  - Cada servicio de `required` existe; `depends_on` solo apunta a declarados; un
    servicio `isolated` no tiene `env_file` ni recibe ninguna `forbidden_env`; su
    `environment:` cubre `requires_env`.
  - Aceptación: reproduce #1 (`env_file`) y #2 (contrato de arranque sin cubrir) en
    árboles mínimos.
  - Verifica: `tests/test_wiring_compose.py`.

- [ ] **W4** (b) `Settings` ↔ `.env.example` y la tabla generada (W14)
  - Cada campo aparece en `.env.example` y cada clave es campo o `external_keys`;
    `example_only` exime de la igualdad de valor. Con `--check`, regenera la región
    marcada de `operacion.md` §2 y compara.
  - Aceptación: una variable que falta en `.env.example` sale como P2; una clave
    sobrante, como P3; la región generada reproduce la versión a mano.
  - Verifica: `tests/test_wiring_settings.py`.

- [ ] **W5** (c) `models.yaml` ↔ `Settings`
  - Cada `${VAR}` de `config/models.yaml` nombra un campo de `Settings`.
  - Aceptación: una referencia rota emite P1 (se resuelve a `""` y el arranque
    *fail-closed* la convertiría en fallo en producción).
  - Verifica: `tests/test_wiring_models.py`.

- [ ] **W6** Generador de la región de `operacion.md` §2
  - Región marcada (`<!-- BEGIN GENERATED: variables -->` … `<!-- END -->`), formato
    de tabla por tema; las descripciones salen de `Field(description=…)`.
  - Aceptación: regenerar sobre el documento real no produce diff inesperado fuera
    de la región; `make docs-check` sigue en verde.
  - Verifica: `tests/test_wiring_docs.py`.
  - Ficheros: `docs/operacion.md` (región), `settings.py` (`description=`).

### Checkpoint 2 (tras W3–W6)

- [ ] Las tres comprobaciones y el generador verdes; cero falsos positivos sobre el
  árbol real.

## Bloque 3 — Gate y regresión (checkpoint C3)

- [ ] **W7** CLI `scripts/wiring_check.py` y `make wiring-check` en `make ci`
  - Salida legible y código de salida 1 si hay hallazgos de severidad ≥ P2 o la
    región generada difiere; `--check` (solo comprobar) y `--write` (regenerar).
  - Aceptación: una deriva introducida a mano en un árbol de prueba falla; el árbol
    real pasa sin ruido.
  - Verifica: `tests/test_wiring_check.py`.

- [ ] **W8** Casos semilla como tests de regresión (criterio 1)
  - #1 y #2 por el motor; #3 (default de `MODELS_CONFIG` frente a la vía `LLM_*`)
    como test puntual (W10), no como regla.
  - Aceptación: los tres fallan con el estado de `008da57` y pasan con el actual.
  - Verifica: los tests de los bloques 2–3.

### Checkpoint 3 (tras W7–W8)

- [ ] `make wiring-check` en `make ci`; los tres casos semilla cubiertos.

## Bloque 4 — Docs y cierre de la fase 1 (checkpoint C4)

- [ ] **W9** Documentos vivos y cierre de fase
  - `desarrollo.md` §2 (zona de tests) y §3 (cómo se extiende con el motor);
    `operacion.md` §2 (región generada) y §1 si el comando se documenta ahí;
    `arquitectura.md` §7 si aparece deuda; `CONTEXT.md` si nace el término «wiring».
  - Aceptación: `make ci` y `make docs-check` en verde; criterios 1–4 y 7–8 de la
    spec §7 verificados.
  - Verifica: `make ci`.

### Checkpoint 4 (tras W9)

- [ ] Fase 1 cerrable; la spec se queda `aprobada` hasta cerrar la fase 2.

## Fase 2 — La persona (paraguas, sin planificar)

- [ ] **W10** `WiringChecker` como `ReviewerPort` (`name = "wiring"`)
  - Adaptador `FileSource` sobre `VcsPort` a `head_sha`, construcción en
    `build_reviewers` solo si el perfil del repo revisado declara `wiring:`,
    `verification: verified_static` y estado de fuente no limpio si un fichero del
    contrato es ilegible. Añadir `wiring` a `profile._MAP_KEYS` y a
    `tests/test_profile.py`.
  - Se abre cuando haya un repo con perfil que declare el contrato (Q4: dos
    *callers* —gate y persona— bastan; no hay artefacto aparte).
  - Verifica: criterios 5–6 de la spec §7.
