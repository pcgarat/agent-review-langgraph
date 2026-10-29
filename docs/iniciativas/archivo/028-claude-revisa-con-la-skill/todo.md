Última modificación: 2026-10-24 | Creado: 2026-10-24

# Todo: Claude revisa con la skill

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). **A va antes que B**: sin adaptador no hay artefacto que
el checkout merezca lanzar.

## Bloque A — Adaptador (checkpoint A-K)

- [x] **A1** Objeto `mode:agent` a lista de hallazgos
  - `complete` con un hallazgo completo devuelve una lista que pasa
    `validate_finding`. El `id` es el `#`. `pre_existing_findings` entran con
    `pre_existing: true`. No se filtra por `actionable_findings`.
  - Un P0–P2 sin `failure_scenario` copia `why_it_matters`. Sin ambos, ese
    hallazgo no entra.
  - `failed`, `skipped`, `degraded`, un JSON que no es el objeto, y un
    `complete` cuyos hallazgos se descartan todos devuelven fallo, no `[]`.
  - Verifica: `make test T=tests/test_claude_artifact.py`.
  - Ficheros: módulo nuevo del adaptador, `tests/test_claude_artifact.py`.

### Checkpoint A-K

- [x] Un hallazgo de fixture se valida y un `failed` no se guarda como vacío.
- [x] `make test T=tests/test_claude_artifact.py` en verde.

## Bloque B — Checkout (checkpoint B-K)

- [x] **B1** Dos extremos, sin remoto
  - `prepare_checkout` recibe `base_sha`, lo trae, deja `review` en el
    `head_sha` y `base` en el `base_sha`, y quita el remoto.
  - Sin `base_sha`, o si el fetch de la base falla, `CheckoutError` y el CLI
    no arranca. El token no queda en `.git/config`.
  - Verifica: `make test T=tests/test_claude_runner.py`.
  - Ficheros: `src/agent_review/claude_runner/checkout.py`,
    `tests/test_claude_runner.py`.

- [x] **B2** `base_sha` en el puerto
  - `ClaudeRunner.run`, el cliente HTTP y `POST /run` lo llevan. El worker lo
    pasa desde el request que ya rellenó `_with_base_sha`.
  - Verifica: `make test T=tests/test_claude_runner.py`.
  - Ficheros: `src/agent_review/ports/claude.py`,
    `src/agent_review/adapters/claude_client.py`,
    `src/agent_review/claude_runner/main.py`,
    `src/agent_review/worker/main.py`, `tests/test_claude_runner.py`.

### Checkpoint B-K

- [x] El árbol de prueba está en el `head_sha`, con la ref `base`, sin remotos.
- [x] `make test T=tests/test_claude_runner.py` en verde.

## Bloque R — Invocación (checkpoint R-K)

- [x] **R1** Prompt, herramientas y techos
  - El argv lleva `/ce-code-review mode:agent base:` más el SHA, la
    prohibición de peer externo, `--setting-sources user`, y no lleva la
    frase «Revisa el árbol de trabajo».
  - `Bash` no está denegado entero. Siguen denegadas edición del checkout,
    `WebFetch` y `WebSearch`. `dontAsk` se mantiene.
  - Defecto de `CLAUDE_MAX_TURNS` 200 y `CLAUDE_TIMEOUT_S` 3600.
  - Verifica: `make test T=tests/test_claude_runner.py`.
  - Ficheros: `src/agent_review/adapters/claude_cli.py`,
    `src/agent_review/settings.py`, `tests/test_claude_runner.py`.

- [x] **R2** Sandbox en la config del runner
  - Al preparar `CLAUDE_CONFIG_DIR` se escribe un `settings.json` con sandbox
    activo, lista estricta solo `api.anthropic.com`, sin salida del sandbox,
    checkout no escribible y `/tmp` sí.
  - Si faltan `bwrap` o `socat`, el proceso no arranca.
  - Verifica: `make test T=tests/test_claude_runner.py`.
  - Ficheros: `src/agent_review/claude_runner/main.py`,
    `tests/test_claude_runner.py`.

- [x] **R3** Imagen con sandbox y skill
  - La etapa `claude-runner` instala `bubblewrap` y `socat`, y copia
    `image/claude/skills/ce-code-review`. Al arrancar, si en el config dir
    falta `SKILL.md`, se vuelca esa copia.
  - El árbol copiado no se edita. El test del Dockerfile ve los paquetes y
    el `SKILL.md`.
  - Verifica: `make test T=tests/test_claude_runner.py`.
  - Ficheros: `Dockerfile`, `image/claude/skills/ce-code-review/` (copia),
    `src/agent_review/claude_runner/main.py`, `tests/test_claude_runner.py`.

### Checkpoint R-K

- [x] El argv y el `settings.json` cumplen la spec §6.1 y §6.8.
- [x] `make test T=tests/test_claude_runner.py` en verde.

## Bloque C — Techo (checkpoint C-K)

- [x] **C1** El runner distingue el techo
  - `ClaudeCeiling` es permanente. El runner responde `504` solo cuando el
    `wait` agota `CLAUDE_TIMEOUT_S` o el CLI sale por `--max-turns`.
  - El cliente ya no convierte ese `504` en `ClaudeTimeout`. Un `502` sigue
    siendo caída reintentable.
  - Verifica: `make test T=tests/test_claude_runner.py`.
  - Ficheros: `src/agent_review/ports/claude.py`,
    `src/agent_review/adapters/claude_client.py`,
    `src/agent_review/adapters/claude_cli.py`,
    `src/agent_review/claude_runner/main.py`, `tests/test_claude_runner.py`.

- [x] **C2** El worker parkea el techo
  - Ante `ClaudeCeiling`, parkea con motivo `ceiling`, avisa una vez y no
    reencola. El login no lo suelta. Un `502` o un JSON inválido siguen el
    reintento de hoy.
  - Verifica: `make test T=tests/test_worker.py`.
  - Ficheros: `src/agent_review/worker/main.py`, `tests/test_worker.py`.

### Checkpoint C-K

- [x] Un `504` no vuelve a la cola. Un `502` sí.
- [x] `make test T=tests/test_worker.py` en verde.

## Bloque T — Transcripción (checkpoint T-K)

- [x] **T1** Fichero entero, con repo
  - Cada evento `stream-json` se redacta y se añade sin cortar la línea ni
    el fichero. La clave incluye `repo`, `pr_id` y el `head_sha` completo.
  - `read_activity` no se queda con los últimos 20 000 caracteres.
  - Verifica: `make test T=tests/test_claude_runner.py`.
  - Ficheros: `src/agent_review/claude_activity.py`,
    `src/agent_review/claude_runner/main.py`,
    `src/agent_review/adapters/claude_cli.py`, `tests/test_claude_runner.py`.

### Checkpoint T-K

- [x] Un secreto de prueba no está en el fichero. La línea larga está entera.
- [x] `make test T=tests/test_claude_runner.py` en verde.

## Bloque P — Panel (checkpoint P-K)

- [x] **P1** SSE y descarga
  - Mientras la ejecución vive, el SSE de la ficha recibe una línea nueva
    sin esperar al cierre. Al terminar, la descarga devuelve el fichero
    entero.
  - Sesión obligatoria, `lector` incluido. Sin sesión, no. La sección Claude
    deja de pintar el log recortado como conversación.
  - Verifica: `make test T=tests/test_web.py` y
    `make test T=tests/test_web_ui.py`.
  - Ficheros: `src/agent_review/web/main.py`, `src/agent_review/web/ui.py`,
    `tests/test_web.py`, `tests/test_web_ui.py`.

### Checkpoint P-K

- [x] El SSE ve una línea a mitad y la descarga no corta a 20 000.
- [x] `make test T=tests/test_web_ui.py` en verde.

## Bloque D — Documentación (checkpoint D-K)

- [x] **D1** ADR y documentos vivos
  - ADR nuevo, número libre en ese momento: la skill corre fuera del núcleo,
    el sandbox es el cierre de `Bash`, y el techo no se reintenta. 0002 y
    0028 quedan aceptados y enlazados, no reescritos.
  - `operacion.md`, `desarrollo.md`, `CONTEXT.md` y `.env.example` dicen los
    techos 200 y 3600, el sandbox y la descarga. No copian el mapeo del
    adaptador.
  - Verifica: `make docs-check` y `make ci`.
  - Ficheros: `docs/adr/`, `docs/adr/README.md`, `docs/operacion.md`,
    `docs/desarrollo.md`, `CONTEXT.md`, `.env.example`,
    `docs/arquitectura.md`.

### Checkpoint D-K

- [x] `make ci` en verde.
- [x] La spec sigue en `aprobada` hasta que el comportamiento esté en el
      código; entonces pasa a `implementada` en el mismo cambio que cierre D1.
