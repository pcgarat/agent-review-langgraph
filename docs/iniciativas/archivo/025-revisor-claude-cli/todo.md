Última modificación: 2026-10-23 | Creado: 2026-10-23

# Todo: Review con Claude CLI

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). El bloque A es la frontera. Con `REVIEW_ENGINE=llm` el
grafo de hoy no se toca.

## Bloque A — Frontera (checkpoint A-K)

- [x] **A1** ADR-0028 del proceso
  - `docs/adr/0028-cli-claude-fuera-del-worker.md` y fila en
    `docs/adr/README.md`. Estado aceptado.
  - El proceso es un servicio distinto del worker y del `sandbox-runner`. El
    checkout es un árbol de trabajo en su propio volumen, no el clon *bare* de
    [ADR-0018](../../../adr/0018-workspace-de-lectura-en-el-runner.md).
  - La credencial de clonado no pasa al subproceso `claude`. Ese subproceso no
    recibe token de VCS ni de Teams, ni herramientas de escritura. La única
    salida que cuenta es stdout.
  - No reescribe el 0002 ni el 0018: el núcleo sigue viendo hallazgos ya
    validados contra `contracts/findings-schema.json`.
  - Verifica: `make docs-check`.
  - Ficheros: `docs/adr/0028-cli-claude-fuera-del-worker.md`, `docs/adr/README.md`.

- [x] **A2** `REVIEW_ENGINE` al arrancar
  - `settings.py`: `review_engine` con valores `llm` y `claude`. Otro valor
    aborta el arranque. Por defecto `llm`.
  - `build_runtime` con `llm` llama a `build_graph` como hoy. Con `claude` aún
    no compila el grafo nuevo (llega en G1): falla en claro si se arranca el
    worker en ese motor antes de G1, en vez de caer al grafo de personas.
  - `.env.example` comentado y fila en `docs/operacion.md` §2.
  - Verifica: `tests/test_review_engine.py` (por defecto `llm`; valor
    desconocido no construye runtime).
  - Ficheros: `src/agent_review/settings.py`, `src/agent_review/worker/main.py`,
    `.env.example`, `docs/operacion.md`, `tests/test_review_engine.py`.

### Checkpoint A-K

- [x] `REVIEW_ENGINE=llm` deja el grafo de hoy.
- [x] Un valor desconocido no arranca.
- [x] ADR-0028 aceptado y enlazado.
- [x] `make ci` en verde.

## Bloque B — Artefacto (checkpoint B-K)

- [x] **B1** Guardar solo un JSON que cumple el esquema
  - Clave `repo + pr_id + head_sha`. La ruta la elige el worker, no el CLI.
  - Validación con el mismo contrato que `domain/contracts.validate_finding`.
  - Un cuerpo inválido, vacío de verdad (no es JSON) o que no es una lista de
    hallazgos no se marca válido. Leer esa clave devuelve «no hay artefacto».
  - Una lista válida vacía sí es artefacto válido.
  - Verifica: `tests/test_claude_artifact.py` (válido, inválido, lista vacía,
    segunda lectura de la misma clave).
  - Ficheros: `src/agent_review/adapters/claude_artifact.py`,
    `src/agent_review/settings.py`, `tests/test_claude_artifact.py`.

### Checkpoint B-K

- [x] El inválido no cuenta como review sin hallazgos.
- [x] La clave distingue dos `head_sha` del mismo PR.

## Bloque G — Grafo (checkpoint G-K)

Depende de B. No depende del binario.

- [x] **G1** `build_claude_graph`
  - Aristas: `scope → persona → merge → stamp → hitl → publish → notify`.
  - `dispatch` manda el cargador del artefacto (persona `claude`) y, si el
    perfil declara `checks.typecheck`, `ToolingReviewer`. No recibe
    `LlmReviewer`, `VerifierPort` ni `FixerPort`.
  - `FindingAction.PUBLISH` (`publish`) entra en `PENDING_ACTIONS` y en
    `FindingAction.pending`. El sello lo usa con `publication=FULL`. La
    política de autonomía no se ejecuta.
  - `build_runtime` compila este grafo cuando `REVIEW_ENGINE=claude`.
  - Verifica: `tests/test_claude_graph.py`. Dobles que lanzan si los llaman
    como revisor LLM o verificador. Un test de aristas de `build_graph` sigue
    viendo `verify` y `fix_dispatch`.
  - Ficheros: `src/agent_review/domain/models.py`,
    `src/agent_review/graph/build.py`, `src/agent_review/worker/main.py`,
    `tests/test_claude_graph.py`, `docs/arquitectura.md` §3–4 y §6.

- [x] **G2** L0 antes de la pausa
  - El cargador tira el hallazgo cuya cita no está en el diff. Ese hallazgo no
    llega a `build_approval`.
  - Verifica: `tests/test_claude_graph.py` (una cita dentro y una fuera; la
    pausa solo lista la de dentro).
  - Ficheros: `src/agent_review/graph/build.py`, `tests/test_claude_graph.py`.

- [x] **G3** La persona estática entra en la misma pausa
  - Con perfil que declara `typecheck`, un hallazgo de `ToolingReviewer` y uno
    del artefacto aparecen los dos en el `ApprovalRequest`.
  - Verifica: `tests/test_claude_graph.py`.
  - Ficheros: `src/agent_review/graph/build.py`, `tests/test_claude_graph.py`.

- [x] **G4** `dropped`, silencio en el PR y aviso corto
  - `DecisionOutcome.DROPPED`. Publish lo trata como `PublicationMode.DROP`: no
    va inline y no aparece en el resumen. `rejected` sigue etiquetado
    «descartado» en el motor `llm`.
  - Estado `silence_without_choice`, escrito solo por este grafo. Con todos en
    `dropped`, `publish` no llama al VCS aunque la cobertura sea parcial. Si
    hay fuente estática sin cubrir, el comentario de fuente no leída se
    mantiene.
  - `notify` encola un aviso corto cuando el PR quedó en silencio. El texto no
    dice «0 hallazgos» como comentario de review.
  - Artefacto válido vacío: no pausa, no escribe en el PR, aviso corto.
  - «Solo bloqueantes»: P0 y P1 se publican; el resto queda en `drop` y fuera
    del resumen.
  - Término `dropped` en `CONTEXT.md`.
  - Verifica: `tests/test_claude_graph.py` y el test de publish parcial del
    motor `llm` (sigue comentando).
  - Ficheros: `src/agent_review/domain/models.py`,
    `src/agent_review/graph/build.py`, `CONTEXT.md`,
    `tests/test_claude_graph.py`.

### Checkpoint G-K

- [x] El doble del grafo no llama a `LlmReviewer` ni a `VerifierPort`.
- [x] L0, la mezcla con la persona estática y el silencio del PR quedan cubiertos.
- [x] Los tests actuales del grafo `llm` siguen verdes, sin tocar el gold set.
- [x] `make ci` en verde.

## Bloque H — Tarjeta (checkpoint H-K)

Depende de G4 (`dropped` y `pause_kind`).

- [x] **H1** Lote que cubre todos los pendientes
  - `ApprovalRequest.pause_kind`. `publication` renderiza tres acciones que
    envían solas: solo bloqueantes (P0 y P1 → `approved`, el resto
    `dropped`), todos `approved`, todos `dropped`. La cuarta marca un hallazgo
    y el Enviar sigue apagado hasta cubrirlos todos.
  - El bot traduce la acción a un `HitlDecision` con `build_decision` antes de
    encolar la reanudación. El grafo no ve el botón.
  - Verifica: `tests/test_claude_card.py` (las tres acciones cubren; una marca
    suelta no envía; el texto del aviso de pausa no lleva secreto).
  - Ficheros: `src/agent_review/domain/models.py`,
    `src/agent_review/teams_bot/cards.py`,
    `src/agent_review/teams_bot/decisions.py`, `tests/test_claude_card.py`.

- [x] **H2** La tarjeta de `llm` no cambia
  - Sin `pause_kind`, los botones siguen siendo Aprobar, Rechazar y Anotar.
  - Verifica: el test de tarjeta ya existente, más un caso en
    `tests/test_claude_card.py` con `pause_kind` ausente.
  - Ficheros: `tests/test_claude_card.py` y el test de tarjeta de 013.

### Checkpoint H-K

- [x] Cada lote es una decisión completa.
- [x] La tarjeta del motor `llm` conserva sus tres verbos.
- [x] `make ci` en verde.

## Bloque R — Proceso (checkpoint R-K)

Depende de B. Puede avanzar en paralelo con H a partir de R1.

- [x] **R1** Puerto `ClaudeRunner`
  - `run` devuelve stdout. Errores distinguibles: tiempo agotado, cupo,
    proceso caído, JSON que el caller todavía no valida.
  - Doble de test que registra cuántas veces se invocó y con qué `head_sha`.
  - Verifica: `tests/test_claude_runner.py`.
  - Ficheros: `src/agent_review/ports/claude.py`, `tests/test_claude_runner.py`,
    `docs/desarrollo.md` §3.

- [x] **R2** Argv y entorno del subproceso
  - El adaptador lanza un ejecutable inyectable. El argv no incluye
    herramientas de escritura y sí incluye el tope de turnos. El entorno del
    hijo no contiene `GITHUB_TOKEN`, `TWG_BBC_TOKEN`, `TWG_TOKEN` ni los
    secretos de Teams.
  - Topes en settings: tiempo de pared y turnos. Fila en `docs/operacion.md`
    §2 y `.env.example`.
  - Verifica: `tests/test_claude_runner.py` con un ejecutable que imprime su
    entorno y sale.
  - Ficheros: `src/agent_review/adapters/claude_cli.py`,
    `src/agent_review/settings.py`, `.env.example`, `docs/operacion.md`,
    `tests/test_claude_runner.py`.

- [x] **R3** El worker llama antes de `invoke`, y no repite
  - Artefacto válido para ese `head_sha`: el doble no se vuelve a llamar y el
    grafo arranca.
  - Stdout inválido o proceso caído: no se guarda como válido, no hay
    `invoke`, el job queda reintentable.
  - Verifica: `tests/test_claude_runner.py` (dos pasadas con la misma clave;
    una pasada con stdout inválido y el doble de grafo sin llamadas).
  - Ficheros: `src/agent_review/worker/main.py`, `tests/test_claude_runner.py`.

- [x] **R4** Servicio `claude-runner` y checkout propio
  - Servicio en `docker-compose.yml` bajo el perfil `claude`. Su entorno no
    incluye el token con el que el worker publica. Volumen de trabajo distinto
    del clon *bare*.
  - El checkout es de solo lectura respecto al VCS: sin remoto de push. El
    subproceso no hereda la credencial de clonado.
  - `docs/operacion.md` §1 describe el servicio y el perfil.
  - Verifica: test del aislamiento de rutas (el directorio de trabajo no es el
    del runner de lecturas) y la lista de variables que el compose pasa al
    servicio.
  - Ficheros: `src/agent_review/claude_runner/main.py`, `docker-compose.yml`,
    `docs/operacion.md`, `tests/test_claude_runner.py`.

### Checkpoint R-K

- [x] Segundo intento con artefacto válido: el doble queda a cero llamadas nuevas.
- [x] El entorno de prueba del subproceso no tiene credencial de VCS ni de Teams.
- [x] Un JSON inválido no entra al grafo.
- [x] `make ci` en verde.

## Bloque S — Sesión y parking (checkpoint S-K)

Depende de A2 y de R1.

- [x] **S1** Lista `reviews:claude:parked`
  - Sin sesión, desencolar un job de este motor lo mueve a la lista y el bucle
    sigue. Un job `llm` detrás se consume en la misma pasada de test.
  - No se hace `invoke`.
  - Verifica: `tests/test_claude_parking.py`.
  - Ficheros: `src/agent_review/queue.py`, `src/agent_review/worker/main.py`,
    `tests/test_claude_parking.py`.

- [x] **S2** Un aviso, sin secreto
  - `GET /session` es sí o no. El worker no lee el fichero del token.
  - El primer job sin sesión encola un aviso con enlace al panel. El segundo
    no encola otro. El cuerpo del aviso no contiene el token de prueba.
  - Verifica: `tests/test_claude_parking.py`.
  - Ficheros: `src/agent_review/worker/main.py`, `tests/test_claude_parking.py`.

- [x] **S3** Login del operador en el panel
  - Formulario solo con `require_operador` y la misma defensa CSRF que los
    otros comandos. El cuerpo no se registra. La respuesta no devuelve el
    token.
  - Al guardarlo en el fichero `0600`, los jobs de `reviews:claude:parked`
    vuelven a `reviews:jobs` y se borra la clave del aviso.
  - Verifica: `tests/test_claude_parking.py` (lector recibe 403; operador
    guarda y la cola principal recibe el job; el log de prueba no contiene el
    token).
  - Ficheros: `src/agent_review/web/main.py`, `src/agent_review/web/ui.py`,
    `tests/test_claude_parking.py`, `docs/operacion.md` §5.

- [x] **S4** Caducidad y cupo
  - Sesión caducada: el runner retira el fichero, `GET /session` pasa a no, el
    siguiente job parkea y el aviso se repite.
  - Cupo agotado: parkea con su aviso, no se reintenta y no entra al grafo.
  - Verifica: `tests/test_claude_parking.py`.
  - Ficheros: `src/agent_review/claude_runner/main.py`,
    `src/agent_review/worker/main.py`, `tests/test_claude_parking.py`.

### Checkpoint S-K

- [x] Sin token, el worker sigue consumiendo jobs `llm` y la outbox tiene un solo aviso.
- [x] El texto del aviso no contiene el token.
- [x] Tras el login del operador, el job parqueado se encola.
- [x] El cupo parkea y no publica.
- [x] `make ci` en verde.

## Cierre

- [x] **D1** Documentos vivos alineados con el código
  - `docs/arquitectura.md`, `docs/operacion.md`, `docs/desarrollo.md` y
    `CONTEXT.md` describen el motor que quedó, sin copiar este plan.
  - La spec pasa a `implementada` y el índice de iniciativas muestra el estado
    final solo cuando G, H, R y S están hechos.
  - Verifica: `make docs-check` y `make ci`.
