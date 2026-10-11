Última modificación: 2026-10-11 | Creado: 2026-10-11

# Fase 3 — Fix + verificación

Especificación del ejecutor de fixes. Un hallazgo que un humano **aprueba** en el
HITL de Fase 2 deja de ser solo una etiqueta ("aprobado"): el agente genera un
parche, lo **verifica en un sandbox con la imagen del CI** y, solo si pasa, lo
publica como **sugerencia inline** en el PR. Si no pasa tras los intentos
permitidos, descarta el trabajo y **escala** con el log. El agente **sigue sin
escribir en el repo**: quien aplica la sugerencia es una persona.

Complementa `docs/diseno-agente-code-review-20261010.md` (§6.4, §8, §9, §13, §15) y
`docs/fase2-hitl-20261011.md`. El estado vivo está en `tasks/plan.md`.

> **Estado: aprobado (2026-10-11) con enmiendas.** Decisiones de arranque: base
> pura (`F1`–`F6`) primero; runner rootless en esta máquina (Q1); la verificación
> **no** bloquea la publicación de otras reviews (R2 revisado); P6 es
> **precondición** del runner (Q4). Las tareas viven en `tasks/todo-fase3.md`.

---

## 1. Objetivo

"Hecho" del roadmap (§13), aterrizado:

> Un fix **aprobado** pasa `stan → deptrac → cs-check → tests dirigidos → make test`
> con la imagen del CI **fijada por digest**, y se publica como sugerencia inline.
> Un fix que falla tras `max_fix_attempts` **no deja rastro en el repo** y escala
> con el `stderr_tail` real.

Usuario: el autor/revisor del PR que ya aprobó el hallazgo con `reviews decide`.
Éxito: puede aplicar la sugerencia con un clic sabiendo que la suite pasa.

### No-objetivo (explícito)

| Fuera | Por qué |
|---|---|
| Commit o push a cualquier rama (del PR o follow-up) | Decisión E1: entrega por sugerencia. Subir de nivel con datos (§14 del diseño) |
| `fix_auto` (sin aprobación) | Fase 4. El techo sube solo a `fix_with_approval` |
| Fixers deterministas (php-cs-fixer, rector) | Decisión E2: LLM primero; el `FixerPort` deja la puerta abierta (Strategy) |
| Fix de hallazgos multi-fichero | Una sugerencia inline toca un rango de un fichero |
| Mutation testing / rector en el sandbox | Fase 4 (§15.4) |
| Egress con allowlist por dominio | v1: red solo en `setup`, `--network none` en los checks (§5.3) |
| Paralelizar fixes o separar cola de fixes | v1 secuencial; riesgo de latencia asumido (§8, R2) |

---

## 2. Decisiones tomadas (2026-10-11)

| # | Decisión | Valor | Consecuencia |
|---|---|---|---|
| E1 | Entrega | **Sugerencia inline** en el PR | Cero escritura en el repo; la saga se reduce a descartar + escalar (§6) |
| E2 | Generación del parche | **Fixer LLM → diff unificado**, validado antes de ejecutar | `suggested_fix` (texto libre) pasa a ser una pista, no el parche |
| E3 | Aislamiento | **Servicio `sandbox-runner` separado** con Docker rootless/Podman | El worker **nunca** toca un socket de Docker; habla HTTP interno |
| E4 | Verificación | **Dirigida + suite completa** | ~20 min por fix en el peor caso; fiel a §9.5 y §13 |
| E5 | Autoridad humana | **D3 invertida (2026-10-11)**: la protección de seguridad manda sobre la aprobación. `never_auto_paths` y `protected_subject` **bloquean** el fix aunque una persona marque `APPROVED` | El humano decide qué *puede* hacer el agente, no amplía lo que tiene *prohibido*; lo bloqueado se publica como `BLOCKED` con su razón (§2.1) |
| E6 | Entrega: **seam** de formato | El nodo `fix` produce un `FixResult` verificado; un `DeliveryFormatter` (Strategy) lo emite como ` ```suggestion ` o ` ```diff ` | R1 deja de ser bloqueante: solo elige el adaptador, no el diseño |
| E7 | Verificación **desacoplada** de la review | La verificación no bloquea la publicación de otras reviews (R2) | La latencia del sandbox sale de la ruta caliente de la cola |

### 2.1.1 E6 con R1

El formato de entrega se aísla tras un `DeliveryFormatter` porque Bitbucket Cloud
**no** tiene una feature de *suggested changes* como GitHub/GitLab (el CLI `twg`
tampoco expone nada de `suggestion`: verificado en `twg bb prs comment create`).
El spike de R1 con un PR real solo decide qué adaptador es el que rinde —el
bloque ` ```suggestion ` o su fallback ` ```diff `—, sin tocar el nodo `fix` ni el
contrato. Un `FixResult` `VERIFIED` es el mismo en ambos casos.

### 2.1.2 E7 con R2

La verificación **no** puede vivir en la ruta de la cola de reviews: un fix de
hasta ~20 min (o ~60 min con `max_fixes_per_pr: 3`) dejaría en cola todas las
demás reviews del worker único. E7 la separa: el trabajo del sandbox se despacha
y la publicación de las demás reviews no espera a que termine. Es una propiedad
de arquitectura, no una optimización, y por eso entra en v1 (revisión de R2).

### 2.1 D3 invertida: la protección manda sobre la aprobación

D3 decía que un único actor podía autorizar un fix en rutas nunca-auto. **Se invierte
(2026-10-11)**: `never_auto_paths` y `protected_subject` son un **bloqueo duro**, no un
gate que la aprobación humana pueda saltar. Aunque una persona marque `APPROVED`, un
hallazgo en esas rutas o temas **no** produce parche ni toca el sandbox: se publica como
`BLOCKED` con su razón (§5.5). El humano decide qué *puede* hacer el agente dentro de lo
permitido; no amplía lo que la política tiene *prohibido*. Es coherente con §8 del diseño
("`protected_subject` bloquea el autofix siempre") y con E1: el agente no tiene token de
escritura, pero la ejecución del parche en el sandbox (código de terceros) es justo lo
que no se delega a la aprobación.

---

## 3. Supuestos (corregir antes del plan)

1. **Bitbucket Cloud renderiza ` ```suggestion ` vía API.** No verificado con
   `twg` 1.3.5 (el CLI no expone ningún concepto de `suggestion`). Con **E6** ya no
   bloquea: el formato vive tras un `DeliveryFormatter`; si Bitbucket no lo
   aplica, su adaptador emite ` ```diff ` y sigue siendo utilizable copiándolo.
2. **El fichero al `head_sha` se lee con `twg bb repo file <path> --ref <sha>`**
   (verificado que el comando existe; falta probarlo contra `emotivoo-v2`).
3. **El runner clona con un token de Bitbucket de solo lectura** (`repository:read`),
   distinto del de `twg` del worker. El worker no clona.
4. **Docker rootless o Podman** está disponible en el host del runner. Si no, el
   runner no arranca: no hay modo "socket del host".
5. **El digest de `emotivoo-ci` se fija en el perfil** y se actualiza a mano (o por
   un job) cuando cambia `Dockerfile.ci`. Un PR que toca `docker/php/Dockerfile.ci`
   **no se verifica**: el fix escala con razón "imagen del CI cambiada por el PR".
6. Tests dirigidos por convención `src/<Ruta>/<Clase>.php` →
   `tests/**/<Clase>Test.php`. Si no existe test, se salta al `make test`.
   **Resuelto (Q2, 2026-10-11):** el comando es `bin/phpunit {targets}`; `bin/phpunit`
   autodescubre `phpunit.dist.xml` (PHPUnit ≥10.1), así que respeta `failOnDeprecation`,
   el bootstrap y `APP_ENV=test` sin `-c`.

---

## 4. Flujo

```
hitl (APPROVED)                  fix                                  publish
   │   ┌──────────────────────────────────────────────────────┐            │
   └──►│ por cada hallazgo aprobado con acción fix_*:         │──────────► │
       │  1. leer fichero @head_sha        (VcsPort)          │  sugerencia│
       │  2. FixerPort → ProposedPatch     (LLM)              │  verificada│
       │  3. validar parche (local, puro)                     │  o escalado│
       │  4. SandboxPort.verify(...)       (runner HTTP)      │  con log   │
       │  5. falla y quedan intentos → 2 con stderr_tail      │            │
       └──────────────────────────────────────────────────────┘            │
```

- **Nodo nuevo `fix`** entre `hitl` y `publish`. Si no hay aprobados con `fix_*`, es
  un no-op (el grafo de Fase 2 queda igual en comportamiento).
- `REJECTED`/`NOTED`/`EXPIRED` no entran en `fix`. `APPROVED` sobre un `escalate`
  **no** genera fix: aprobar un escalado significa "procede fuera del agente".
  Solo `proposed_action ∈ {fix_with_approval, fix_auto}` + `APPROVED`.
- Secuencial dentro del nodo; tope `guards.max_fixes_per_pr` (nuevo, por defecto 3).
  Los que excedan el tope se publican como `report` con la razón.

---

## 5. Componentes

### 5.1 Contrato (`domain/models.py`)

```python
class FixStatus(StrEnum):
    VERIFIED = "verified"           # pasa todo; se publica como sugerencia
    FAILED = "failed"               # agota intentos; escala con log
    INVALID_PATCH = "invalid_patch" # el parche no pasa la validacion local
    NOT_VERIFIABLE = "not_verifiable"  # imagen cambiada por el PR, sin digest, runner caido
    SKIPPED = "skipped"             # tope max_fixes_per_pr o presupuesto

class ProposedPatch(BaseModel):
    finding_id: str
    file: str
    diff: str                       # unificado, un solo fichero
    start_line: int                 # rango (lado nuevo) que reemplaza la sugerencia
    end_line: int
    replacement: str                # texto de la sugerencia

class FixResult(BaseModel):
    finding_id: str
    status: FixStatus
    attempts: int
    patch: ProposedPatch | None
    checks: list[CommandResult]     # tails, nunca el log completo
    image_digest: str | None
    reason: str = ""
```

`ReviewState.fixes: dict[str, FixResult]` con reducer de merge por `finding_id`
(como `_merge_sources`). `ReviewOutcome.fixes` se expone igual.

### 5.2 `FixerPort` (Strategy, E2)

```python
class FixerPort(Protocol):
    def propose(
        self, finding: Finding, *, source: str, previous: FixResult | None
    ) -> ProposedPatch: ...
```

- Adaptador `LlmFixer` sobre el `LlmPort` existente (mismo presupuesto
  `budget_tokens_per_pr`).
- Entrada: el hallazgo, el fichero completo al `head_sha` y, en el segundo
  intento, los `stderr_tail` del primero. **Nunca** el `.env`, el perfil completo ni
  salidas de `setup`.
- Errores del LLM → `is_transient` como en las personas.

**Validación local (pura, sin red)** antes de gastar un minuto de sandbox:
un solo fichero = `finding.file`; un solo hunk; ≤ `guards.max_fix_lines`
(nuevo, por defecto 30); el rango cae en líneas **añadidas del diff del PR**
(`resolve_inline_anchor`), porque una sugerencia solo puede reemplazar lo que el
PR muestra. Falla → `INVALID_PATCH`, cuenta como intento.

### 5.3 `SandboxPort` y servicio `sandbox-runner` (E3)

El puerto actual (`run(recipe, *args)`) asume un workspace vivo en el worker. Se
sustituye por una única operación de alto nivel, para que el ciclo de vida del
workspace (clonar → aplicar → ejecutar → destruir) viva **entero** en el runner:

```python
class VerifyRequest(BaseModel):
    run_key: str              # review_key:finding_id:intento — idempotencia
    repo: str
    head_sha: str
    patch: str
    recipes: list[str]        # nombres del perfil, en orden; para al primer fallo
    targets: list[str] = []   # rutas de test para la recipe dirigida (validadas)

class VerifyReport(BaseModel):
    run_key: str
    image_digest: str
    results: list[CommandResult]
    passed: bool

class SandboxPort(Protocol):
    def verify(self, request: VerifyRequest) -> VerifyReport: ...
```

- `HttpSandboxAdapter` (worker): `POST /verify` **bloqueante** con un `run_key`
  idempotente; el timeout del POST es `sandbox.timeout_s` más una holgura. Un
  `POST` con un `run_key` ya visto devuelve el informe existente: si el worker cae
  a mitad, al reanudar **no** se lanza una segunda ejecución (R8).
  **Enmienda (2026-10-11):** se descarta `POST /runs` + sondeo `GET /runs/{id}`
  (que decía esta sección). El worker es secuencial, así que el sondeo no saca la
  verificación de la ruta de la review: solo añade estado de runs y GC. El
  desacople *fuerte* (E7) es otra cosa —una cola/consumidor de fixes aparte— y
  sigue pendiente; el adaptador síncrono no lo finge.
- **Runner** (`src/agent_review/sandbox_runner/`, FastAPI interno, sin puerto publicado):
  1. Rechaza un perfil sin `ci_image_digest` o con tag mutable.
  2. Clona `repo@head_sha` (token de solo lectura) en un directorio efímero.
  3. `git apply --check` + `git apply` del parche.
  4. `setup` **con red** y `composer install --no-scripts` (no ejecuta código del
     PR con red); el resto de `setup` y todos los checks con `--network none`.
  5. Recipes en orden, argv sin shell, cada una con su timeout; CPU/mem limitados;
     sin `docker.sock`, sin privilegios, usuario no root.
  6. **Siempre** destruye contenedor y workspace (`finally`).
- Cachés `vendor/` y `var/tailwind` por hash de su clave (§15.5), montadas de solo
  lectura y copiadas al workspace. **Pendiente (v1):** se ejecuta `composer install`
  en cada verificación; el montaje de cachés llega cuando el coste lo pida.
- Concurrencia 1 en v1.

#### Piezas del servicio (F8)

La orquestación se separa en puertos para poder verificar **sin Docker** en `make ci`:

| Pieza | Rol |
|---|---|
| `sandbox_runner/main.py` | FastAPI: `POST /verify` (bloqueante, idempotente por `run_key`) y `GET /healthz`. Bearer compartido worker↔runner. |
| `sandbox_runner/verifier.py` | Orquesta el ciclo (imagen → clon → parche → setup → checks → destruir). Rechaza perfil sin digest, valida `targets`, cachea por `run_key` y rechaza un run en curso. |
| `sandbox_runner/workspace.py` | `GitWorkspace`: clona `@head_sha` con el token de solo lectura y aplica el parche (`--check` primero). `WorkspacePort` permite un fake en tests. |
| `sandbox_runner/runtime.py` | `ContainerRuntime`: `create`/`start`/`remove` explícitos (no un `run --rm` opaco) para destruir siempre en `finally` y contar creaciones/destrucciones. `ContainerSpec` fija imagen por digest, red, `--cap-drop ALL`, `no-new-privileges`, usuario no root y límites. |
| `sandbox_runner/recipes.py` | Expansión de recipes y allowlist de `targets`; `argv` sin shell (`shlex.split`, nunca `shell=True`). |

### 5.4 Recipes y perfil

`config/repo_profile.emotivoo-v2.yaml` gana:

```yaml
ci_image_digest: "sha256:…"          # obligatorio para verificar (solo sha256)
verify:                               # el perfil es la fuente unica del orden
  - targeted                          # tests dirigidos (bin/phpunit {targets})
  - typecheck                         # make stan (tras cache:clear del setup)
  - arch                              # make deptrac
  - lint                              # make cs-check
  - tests                             # make test
checks:
  targeted: "bin/phpunit {targets}"   # Q2: rutas de test; autodescubre phpunit.dist.xml
setup_network:                        # los setup con red (pueden bajar dependencias)
  - composer install … --no-scripts
setup:                                # el resto del setup, sin red
  - bin/console cache:clear -n
  - bin/console assets:install -n
  - bin/console importmap:install -n
```

`targeted` va **antes** de `typecheck`: es la señal más barata y local (tests del
área tocada) y las reglas custom de PHPStan (`ApplicationServiceMustHaveTestRule`)
exigen el test que el fix ya debería traer. `fix_recipes` en `settings.py` deja de
ser una segunda fuente de verdad: pasa a fallback para entornos sin perfil, y el
worker lee el orden del `verify` del perfil (`profile.fix_recipes`).

`setup` se parte en dos listas por la red: `setup_network` corre con red (es donde
`composer install` baja dependencias, siempre `--no-scripts` para no ejecutar
código del PR) y `setup`, junto con todos los checks, con `--network none` (R5).

Una recipe nueva es una decisión humana (§9.4): el runner solo ejecuta nombres del
perfil, y los `targets` se validan contra una allowlist (`tests/**.php`, sin `..`,
sin rutas absolutas) antes de montar el contenedor.

### 5.5 Publicación

El formato del bloque entregado lo decide un `DeliveryFormatter` (E6):

- `SuggestionFormatter` → ` ```suggestion ` (si R1 se verifica).
- `DiffFormatter` → ` ```diff ` (fallback verificado).

El resto no cambia:

- `VERIFIED` → el inline del hallazgo lleva el bloque del formatter (rango
  `start_line..end_line`) y una línea "verificado: stan, deptrac, cs, N tests ·
  imagen `sha256:…`". Usa la **misma marca de dedupe por `finding_id`** que hoy:
  una reentrega no duplica.
- `FAILED`/`INVALID_PATCH`/`NOT_VERIFIABLE` → inline normal + bloque "fix no
  verificado" con el check que falló y su `stderr_tail` (acotado), y aviso a Teams.
  **No** se vuelve a pausar (evita bucles de decisión).
- `SKIPPED` → inline normal con la razón.
- `BLOCKED` → inline normal con "bloqueado por seguridad: <ruta nunca-auto|tema
  protegido>". Nunca hay parche ni ejecución, ni con `APPROVED` (§2.1).
- El resumen gana una sección "Fixes" con el recuento por estado.

---

## 6. Saga / compensación

Con E1 no hay commit que revertir. La compensación es:

1. El runner destruye workspace y contenedor en cualquier salida (éxito, fallo,
   timeout, cancelación).
2. Un fallo nunca publica el parche como sugerencia aplicable.
3. Escalado: el hallazgo vuelve al humano por Teams + inline con el log.

Si en el futuro se habilita push (E1 revisado), aquí entra el revert real y una
especificación nueva.

---

## 7. Seguridad (delta sobre §8 y §9.6 del diseño)

| Superficie | Control |
|---|---|
| Código del PR ejecutándose | Solo en el runner; red cortada en checks; `--no-scripts` en el `composer install` con red |
| Escape del contenedor | Rootless/Podman, sin privilegios, `--cap-drop ALL`, `no-new-privileges`, usuario no root |
| Worker comprometido | No tiene socket Docker ni credenciales de registro/clonado; solo HTTP al runner |
| Runner expuesto | Solo red interna de compose; token compartido worker↔runner |
| Prompt injection vía el fichero del PR | El LLM solo produce un diff; la validación local acota fichero, hunk y tamaño; nada se aplica al repo sin un humano |
| Secretos al modelo | El fixer ve fichero + hallazgo + tails; nunca env ni perfil |
| Auditoría | Por fix: `actor` que aprobó, hash del parche, digest de la imagen, argv y `exit_code` de cada recipe (log estructurado + `ReviewOutcome.fixes`) |

Credenciales nuevas (entran en `docs/gestion-de-secretos-20261011.md`):
`SANDBOX_GIT_TOKEN` (solo lectura), `SANDBOX_REGISTRY_USER/TOKEN` (pull de
`crg.apkg.io`, se mueven del worker al runner), `SANDBOX_RUNNER_TOKEN`.

---

## 8. Riesgos

| # | Riesgo | Mitigación | Estado |
|---|---|---|---|
| R1 | Bitbucket no renderiza ` ```suggestion ` por API | Spike primero; fallback ` ```diff ` | Abierto |
| R2 | Worker secuencial bloqueado ~20 min por fix | **E7: verificación desacoplada de la ruta de la review (v1)**; `max_fixes_per_pr: 3` | Resuelto en diseño (F5b/F7) |
| R3 | Drift: `emotivoo-ci` cambia y el digest queda viejo | El digest vive en el perfil; un PR que toca `Dockerfile.ci` no se verifica | Mitigado |
| R4 | El LLM produce parches que no aplican o tocan otra cosa | Validación local antes del sandbox; 2 intentos | Mitigado |
| R5 | `composer install` necesita red y ejecuta código | `--no-scripts` con red; scripts después sin red | Mitigado |
| R6 | Flakiness de la suite marca un fix bueno como fallido | Se reporta como `FAILED` con log; no hay reintento ciego | Asumido |
| R7 | Docker rootless no disponible en el host | El runner no arranca; Fase 3 bloqueada, no degradada a socket | Bloqueante |
| R8 | Reanudación tras caída relanza el sandbox | `run_key` idempotente en el runner | Mitigado |

---

## 9. Criterios de hecho

1. Un hallazgo `fix_with_approval` aprobado produce un `FixResult` `VERIFIED` cuya
   verificación ejecutó, en orden, `targeted → typecheck → arch → lint → tests` con la
   imagen **por digest**, y se publica como sugerencia inline **una sola vez**
   (reentrega = no-op).
2. Un parche que rompe un check se reintenta **una** vez con el `stderr_tail`; si
   vuelve a fallar → `FAILED`, sin sugerencia aplicable, con escalado a Teams.
3. Un parche que toca otro fichero, varios hunks o fuera de las líneas del diff →
   `INVALID_PATCH` **sin** llamar al runner.
4. Un perfil sin `ci_image_digest` o con tag `:latest` → `NOT_VERIFIABLE`, sin ejecutar.
5. El runner destruye el workspace en éxito, fallo y timeout (test con un fake de
   Docker que cuenta creaciones/destrucciones).
6. Ningún check se ejecuta con red (verificado en los argv del runner).
7. El worker no tiene `docker.sock` ni credenciales de clonado/registro
   (`docker-compose.yml` + test de settings).
8. `REJECTED`, `NOTED`, `EXPIRED` y `APPROVED` sobre `escalate` no invocan al fixer.
9. **Cero** escrituras en el repo: no existe en el código un camino a
   `commit`/`push`/`branch create`.
10. Smoke real: un PR de `emotivoo-v2` con un hallazgo aprobado acaba en sugerencia
    verificada (o en `FAILED` justificado) contra Bitbucket.

---

## 10. Preguntas abiertas

- **Q1 — resuelto (2026-10-11).** Se monta un demonio Docker **rootless dedicado**
  en esta máquina. Verificado: `/usr/bin/dockerd-rootless.sh` y
  `dockerd-rootless-setuptool.sh` presentes, `unprivileged_userns_clone=1`,
  `max_user_namespaces=116222`. El demonio actual **no** es rootless (sin
  `Security Options`), así que el runner usa su propio demonio. No hay Podman.
- **Q2 — resuelto (2026-10-11).** El comando es `bin/phpunit <rutas…>` (sin `make`),
  con `EXEC_PHP=""` desde el sandbox. `bin/phpunit` **autodescubre `phpunit.dist.xml`**
  (PHPUnit ≥ 10.1; el repo usa 12.5), así que hereda `failOnDeprecation`/`Notice`/
  `Warning`, el `bootstrap` y `APP_ENV=test` sin pasar `-c`. Acepta ficheros o
  directorios (`vendor/bin/phpunit tests/Foo/BarTest.php`); con varias rutas, una por
  argumento. No se usa `make test-unit` (filtra por `--group`, no por fichero) ni
  `paratest` (el objetivo es un test dirigido determinista, no el reparto en procesos).
  En el perfil queda como `checks.targeted: "bin/phpunit {targets}"`.
- **Q3** ¿Quién emite el token de Bitbucket de solo lectura para el runner, y con qué
  rotación?
- **Q4 — resuelto (2026-10-11).** P6 (revisar la heurística de tema protegido con
  seguridad) se mantiene como **precondición** para arrancar el runner: aunque con
  E1 nada se escribe, el runner **ejecuta código de PRs de terceros**. Ya no es
  "requisito previo a escribir" sino "requisito previo a ejecutar".

---

## 11. Comandos

```
make ci                                   # ruff + format + mypy strict + pytest
uv run --extra dev pytest tests/test_fix*.py tests/test_sandbox*.py
docker compose up -d sandbox-runner       # nuevo servicio (rootless)
uv run reviews decide <review_key> --actor <quien> --approve <finding_id>   # dispara un fix
```

Estructura: `src/agent_review/ports/{fixer,sandbox}.py`, `src/agent_review/adapters/{llm_fixer,
http_sandbox}.py`, `src/agent_review/fix/` (validación + orquestación del nodo),
`src/agent_review/sandbox_runner/` (servicio), tests en `tests/` con el mismo patrón que
Fase 2 (fakes de puertos, sin red ni Docker en `make ci`).
