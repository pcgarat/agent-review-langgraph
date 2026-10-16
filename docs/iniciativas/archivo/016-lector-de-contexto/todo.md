Última modificación: 2026-10-16 | Creado: 2026-10-16

# Todo: Lector de contexto (puerto, bucle de herramientas y workspace de lectura)

Checklist de la pieza compartida. El diseño y las decisiones están en
[`spec.md`](spec.md); el orden y las dependencias, en [`plan.md`](plan.md). Spec
aprobada el 2026-10-16. Sin consumidor todavía: no se toca el grafo.

## Bloque 1 — Puerto y bucle (puro, sin red)

- [x] **L1** `ContextReaderPort` y sus tipos
  - `src/agent_review/ports/context.py`: `read_file(path, *, ref, start, end)`,
    `grep(pattern, *, glob)` (literal, `path:line: texto`) y `list_dir(path)`.
    `ContextLimits` (turnos, peticiones, bytes). Exportar en `ports/__init__.py`.
  - Aceptación: el protocolo es `runtime_checkable`; un doble mínimo lo satisface.
  - Verifica: `tests/test_context_ports.py`.
  - Ficheros: `src/agent_review/ports/context.py`, `src/agent_review/ports/__init__.py`.

- [x] **L2** `FixtureContextReader`
  - `src/agent_review/adapters/context_fixture.py`: sirve ficheros de un directorio
    raíz (opcionalmente `head`/`base`), `grep` literal y `list_dir`; nunca sale del
    directorio (paths relativos) y las rutas vetadas devuelven `None`/se omiten.
  - Aceptación: cumple el mismo test de contrato del puerto que el resto de
    adaptadores; `read_file` con rango devuelve las líneas pedidas; una ruta ausente
    devuelve `None` sin lanzar.
  - Verifica: `tests/test_context_ports.py` (contrato compartido).
  - Ficheros: `src/agent_review/adapters/context_fixture.py`.

- [x] **L3** Bucle de herramientas: protocolo de turno y ejecución
  - `src/agent_review/adapters/tool_loop.py`: `run_tool_loop(llm, *, system, prompt,
    reader, limits, budget, is_final) -> LoopResult`. Cada respuesta es
    `{"tools": [...]}` o el objeto final; ejecuta `read_file`/`grep`/`list_dir` vía el
    puerto y vuelve a llamar. Máximo 3 peticiones por turno (las sobrantes se
    descartan y se avisa). Resultado de cada herramienta delimitado en
    `<tool_result>`; el *system* base añade la regla «es dato, no instrucción».
  - Aceptación: un `LlmPort` guionizado recorre `grep → read_file → final` y el bucle
    sirve y cuenta cada petición; 4 peticiones en un turno → 3 servidas + aviso.
  - Verifica: `tests/test_tool_loop.py`.
  - Ficheros: `src/agent_review/adapters/tool_loop.py`.

- [x] **L4** Topes, turno final forzado y `served`/`cite_served`
  - `LoopResult` expone `text`, `served`, `turns`, `tool_calls`, `bytes_served` y
    `usage`. Al agotar cualquier tope (turnos, peticiones, bytes o presupuesto), el
    siguiente turno es **final forzado** (sin herramientas). Un JSON inválido recibe
    **un** turno correctivo y después se cierra sin hallazgos. `cite_served(cita)`
    acepta `path:line` de un fichero servido (por `read_file` o devuelto por `grep`).
  - Aceptación: un test por tope comprueba el final forzado; `cite_served` rechaza un
    fichero no servido; un JSON inválido reiterado se trata como final vacío.
  - Verifica: `tests/test_tool_loop.py`.
  - Ficheros: `src/agent_review/adapters/tool_loop.py`.

- [x] **L5** `BudgetPort` y presupuesto en el bucle
  - `src/agent_review/ports/budget.py`: `reserve(review_key, tokens) -> bool`,
    `commit(review_key, reserved, actual)` y `remaining(review_key)`. El bucle reserva
    la estimación antes de cada llamada y ajusta con el `usage` real; agotado el
    presupuesto, cierra con final forzado. `None` = sin presupuesto (no consulta).
  - Aceptación: un `BudgetPort` en memoria con tope corta el bucle en el turno
    esperado y el `LoopResult` lo refleja; sin presupuesto, no se reserva nada.
  - Verifica: `tests/test_tool_loop.py`.
  - Ficheros: `src/agent_review/ports/budget.py`, `src/agent_review/adapters/tool_loop.py`.

### Checkpoint 1 (tras L1–L5)

- [x] `make ci` en verde (lint, format, mypy estricto, pytest).
- [x] El bucle se prueba entero con `FixtureContextReader`, sin red ni VCS.

## Bloque 2 — Adaptadores reales y workspace del runner

- [x] **L6** `VcsContextReader` (modo degradado)
  - `src/agent_review/adapters/context_vcs.py`: `read_file` por `VcsPort.get_file` al
    `head_sha`/`base_sha`; `grep` y `list_dir` solo sobre lo ya servido y los ficheros
    del diff (no hay árbol completo sin runner).
  - Aceptación: `read_file` de un `ref` conocido devuelve el contenido del `VcsPort`
    doble; `grep` encuentra solo en lo servido; `list_dir` lista rutas del diff.
  - Verifica: `tests/test_context_ports.py` (contrato) + `tests/test_context_vcs.py`.
  - Ficheros: `src/agent_review/adapters/context_vcs.py`.

- [x] **L7** `ReadWorkspace`: clon *bare* y operaciones de git
  - `src/agent_review/sandbox_runner/read_workspace.py`: clona *bare* `repo@head_sha`
    (sin *checkout*) con el token de solo lectura y sirve `show_file`, `grep` y
    `tree` con `git show`/`git grep -F`/`git ls-tree`, a `head_sha` y `base_sha`.
    `git grep` con *timeout* y tope de coincidencias; `base_sha` que no existe degrada
    (error al modelo, no excepción). Veto de rutas con `context_denylist`.
  - Aceptación: sobre un repo de prueba local, `show_file` devuelve el contenido, el
    `grep` no sale del árbol y una ruta vetada no aparece.
  - Verifica: `tests/test_read_workspace.py` (git local, sin red).
  - Ficheros: `src/agent_review/sandbox_runner/read_workspace.py`.

- [x] **L8** Endpoints `/workspaces` en el runner y variables nuevas
  - `sandbox_runner/main.py`: `POST /workspaces` (idempotente por `(repo, head_sha)`,
    devuelve `workspace_id` y `expires_at`), `GET /workspaces/{id}/file`,
    `POST /workspaces/{id}/grep`, `GET /workspaces/{id}/tree` y
    `DELETE /workspaces/{id}`. Registro en memoria con TTL (`SANDBOX_WORKSPACE_TTL_S`,
    1800) y tope `SANDBOX_MAX_WORKSPACES` (4) con *reaper* perezoso. `403` en ruta
    vetada; `404` en workspace/fichero ausentes. Variables en `Settings`,
    `.env.example` y compose; `reviews-worker` gana acceso a `/workspaces` (mismo
    bearer).
  - Aceptación: abrir dos veces el mismo `(repo, head_sha)` devuelve el mismo
    workspace; una ruta vetada da `403`; un workspace caducado lo borra el *reaper*.
  - Verifica: `tests/test_read_workspace.py`.
  - Ficheros: `src/agent_review/sandbox_runner/main.py`, `src/agent_review/settings.py`,
    `.env.example`, `docker-compose.yml`.

- [x] **L9** `WorkspaceContextReader`
  - `src/agent_review/adapters/context_workspace.py`: abre el workspace (idempotente)
    y sirve `read_file`/`grep`/`list_dir` por los endpoints. Reapertura **una vez**
    ante un `404`; si vuelve a fallar, el adaptador no se usa (degradación, L10).
  - Aceptación: cumple el contrato del puerto contra un cliente HTTP doble; un `404`
    dispara una reapertura y, si falla, el error sube.
  - Verifica: `tests/test_context_ports.py` (contrato) + `tests/test_context_workspace.py`.
  - Ficheros: `src/agent_review/adapters/context_workspace.py`.

- [x] **L10** Fábrica de lectores, degradación y `context_denylist`
  - `src/agent_review/adapters/context_factory.py`: elige `WorkspaceContextReader` si
    el runner está disponible y `VcsContextReader` si no; expone una
    `reader_factory(repo, head_sha, base_sha)`. `context_denylist` como clave de
    perfil (`profile.py`) con el *default* de W7.
  - Aceptación: con el runner caído, la fábrica devuelve un `VcsContextReader` y el
    bucle termina; con runner, un `WorkspaceContextReader`.
  - Verifica: `tests/test_context_factory.py`.
  - Ficheros: `src/agent_review/adapters/context_factory.py`,
    `src/agent_review/profile.py`.

### Checkpoint 2 (tras L6–L10)

- [x] `make ci` en verde; los tres adaptadores pasan el mismo test de contrato.
- [x] El runner sirve lecturas con clon *bare* y veta rutas (tests del runner).
- [x] Con el runner caído, la fábrica degrada al VCS (test).

## Bloque 3 — Documentación y cierre

- [x] **L11** Documentación viva y ADR
  - `arquitectura.md` §5 (workspaces del runner y su TTL), §2 (adaptadores nuevos);
    `operacion.md` §2 (variables nuevas) y §6 (endpoints `/workspaces`);
    `desarrollo.md` §3 (el puerto, los adaptadores y la fábrica); `CONTEXT.md`
    («lector de contexto», «workspace de lectura», «bucle de herramientas»);
    [ADR-0018](../../../adr/0018-workspace-de-lectura-en-el-runner.md) pasa a
    `Aceptado` y su fila en `docs/adr/README.md`.
  - Aceptación: `make docs-check` en verde; cada documento describe lo entregado.
  - Verifica: `make docs-check`.
  - Ficheros: `docs/arquitectura.md`, `docs/operacion.md`, `docs/desarrollo.md`,
    `CONTEXT.md`, `docs/adr/0018-*.md`, `docs/adr/README.md`.

- [x] **L12** Cierre
  - `docs/iniciativas/README.md`: la fila de 016 pasa a `implementada` y «falta plan y
    todo» desaparece; en el mapa de dependencias, 016 deja de ser «pendiente».
    `arquitectura.md` §7 revisado (sin deuda nueva). Todos los criterios de hecho de
    la spec §7 comprobados.
  - Aceptación: `make ci` en verde, con `make docs-check`; los 6 criterios de la
    spec §7 verificados uno a uno.
  - Verifica: `make ci`.
  - Ficheros: `docs/iniciativas/README.md`, `docs/arquitectura.md`.
