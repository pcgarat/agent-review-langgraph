Última modificación: 2026-10-16 | Creado: 2026-10-16

# Plan: Lector de contexto (puerto, bucle de herramientas y workspace de lectura)

Una pieza compartida, sin consumidor todavía: el *qué y por qué* vive en
[`spec.md`](spec.md) y las tareas, en [`todo.md`](todo.md). Este plan fija el **orden**,
las dependencias y los checkpoints. 016 **no** toca el grafo ni el contrato de
hallazgo: entrega el puerto, el bucle, los adaptadores y el workspace, probados con
fakes, para que [009](../../009-verificador-adversarial/spec.md) y
[014](../../014-revisor-con-herramientas/spec.md) los consuman después.

## Overview

Tres piezas:

1. `ContextReaderPort` con tres adaptadores —`FixtureContextReader` (ficheros de un
   directorio, para el *replay*), `VcsContextReader` (por `VcsPort.get_file`, modo
   degradado) y `WorkspaceContextReader` (por `/workspaces` del runner)—.
2. `adapters/tool_loop.py`: un bucle *ReAct* en JSON sobre `LlmPort` con topes y
   comprobación de citas, compartido por 009 y 014.
3. El **workspace de lectura** del `sandbox-runner`
   ([ADR-0018](../../../adr/0018-workspace-de-lectura-en-el-runner.md)): clon *bare*
   servido por `git show`/`git grep`/`git ls-tree`, con TTL, tope de workspaces y
   rutas vetadas.

Nada de esto ejecuta código del PR ni entra en la ruta del fix.

## Decisiones de implementación

- **El puerto es puro** (`ports/context.py`): `read_file` (con `ref` y rango),
  `grep` y `list_dir`. Los adaptadores no comparten base: cada uno sirve de donde
  puede (W1).
- **`FixtureContextReader` en `src/`** (Q2 resuelta): lo usa el *replay* en rutas de
  producto y `src/` no debe importar de `tests/`.
- **`grep` literal** (Q1 resuelta): `git grep -F` y búsqueda literal en memoria.
  Cierra el riesgo R4 (patrones caros) y es determinista para el *replay*.
- **Un solo protocolo de turno** (W2): cada respuesta es `{"tools": [...]}` o el
  objeto final que reconoce `is_final`. Máximo **3** peticiones por turno; las que
  sobran se descartan y se avisa al modelo. **Un** turno correctivo ante JSON
  inválido; después, cierre. **Un** turno final forzado al agotar cualquier tope.
- **`served: dict[path, ref]`** (W3): el bucle registra lo servido (por `read_file` y
  por las rutas que devuelve `grep`) y expone `cite_served`; los consumidores validan
  sus citas con él, sin reimplementarlo.
- **El resultado de cada herramienta va delimitado** (`<tool_result>`) y el *system
  prompt* base dice que es dato, no instrucción (W4). Es la defensa común contra la
  inyección desde un fichero del PR.
- **El presupuesto es de 017**: el bucle acepta un `BudgetPort | None`
  (`ports/budget.py`, acotado a la review, con la clave fijada en el adaptador). 016
  solo lo **consume** (reserva antes de cada llamada, ajusta con el `usage` real);
  017 aporta el adaptador Redis. Sin presupuesto (`None`) el bucle no lo consulta.
- **El workspace vive en la memoria del runner** (W6): apertura idempotente por
  `(repo, head_sha)`, TTL de `SANDBOX_WORKSPACE_TTL_S` (1800 s) y un máximo de
  `SANDBOX_MAX_WORKSPACES` (4) abiertos, con *reaper* perezoso. No viaja en el estado
  ni necesita coordinación entre consumidores.
- **Rutas vetadas en el runner** (W7): `context_denylist` del perfil (default
  `.env*`, `*.pem`, `*.key`, `**/secrets/**`); una ruta vetada da `403` y no aparece
  en el `grep`. El veto lo aplica el runner, no el adaptador: pedir otra ruta no lo
  salta.
- **Degradación** (W8): la fábrica (`adapters/context_factory.py`) elige el workspace
  si el runner está disponible; sin runner, o tras un `404` y una reapertura fallida,
  cae a `VcsContextReader`. Un fallo del lector dentro del bucle se sirve al modelo
  como error y la review sigue.
- **Sin consumidor todavía**: 016 no cablea el bucle en el grafo. La fábrica y los
  adaptadores se prueban directamente; [014](../../014-revisor-con-herramientas/spec.md)
  los conecta a `LlmReviewer`.

## Dependency graph

```
L1  ContextReaderPort + tipos (puro)
     └── L2  FixtureContextReader ───────────────┐
                                                  │
L3  tool_loop: protocolo de turno + herramientas ─┤
     ├── L4  topes + final forzado + served/cite_served
     └── L5  BudgetPort + presupuesto en el bucle

L6  VcsContextReader (por VcsPort) ───────────────┐
L7  ReadWorkspace en el runner (clon bare) ──┐     │
     └── L8  endpoints /workspaces + variables│     │
              └── L9  WorkspaceContextReader ─┘─────┤
                                                     │
L10 fábrica + degradación + denylist del perfil ─────┘

L11 documentación viva (ADR, arquitectura, operación, desarrollo, glosario)
L12 cierre (índices, spec implementada, make ci)
```

L1–L2 son el cimiento puro: el bucle (L3–L5) se prueba entero con el lector de
fixtures, sin red. L6 es independiente de L7–L9 (dos formas de servir el mismo
puerto). L10 cierra la degradación, que necesita L6 y L9.

## Task list

Ver [`todo.md`](todo.md) (L1–L12). Orden por dependencia: el puerto y el bucle
primero (lo compartido), los adaptadores reales después, la fábrica al final, y los
docs con el código.

## Checkpoints

- **Tras L1–L2:** el puerto y el `FixtureContextReader` pasan el mismo test de
  contrato; `make ci` en verde.
- **Tras L3–L5:** el bucle respeta cada tope y hace el turno final forzado (un test
  por tope); `cite_served` rechaza una cita no servida; el presupuesto corta el bucle.
- **Tras L6–L9:** el runner abre un workspace *bare*, sirve fichero/`grep`/árbol y
  veta rutas; el `WorkspaceContextReader` y el `VcsContextReader` pasan el test de
  contrato de cada adaptador.
- **Tras L10–L12:** la fábrica degrada al VCS sin runner; `make ci` y `make docs-check`
  en verde; ADR-0018 `Aceptado`; documentos vivos al día.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| `git fetch` de `base_sha` falla en el remoto | Se tolera: sin base, `ref="base"` devuelve error al modelo y la review sigue (W8) |
| `grep` con salida enorme | Literal, con *timeout*, pathspec del `glob` y tope de coincidencias (R4) |
| Disco del runner con clones grandes | Clon *bare*, tope `SANDBOX_MAX_WORKSPACES`, TTL y *reaper* (R3) |
| El bucle no sigue el protocolo | Un turno correctivo y cierre; los modelos candidatos se eligen midiendo (R3 de 014) |
| Inyección de *prompt* desde el contenido | Delimitación `tool_result` + regla en el *system prompt* + veto de rutas (W4, W7) |
| El presupuesto del bucle diverge de 017 | El puerto se define acotado a la review; 016 solo lo consume. 017 implementa el adaptador |
| `mypy` estricto con datos JSON sin tipar | Los objetos del turno se validan con guardas explícitas y `Any` confinado al parseo |

## Dependencias externas

- [ADR-0018](../../../adr/0018-workspace-de-lectura-en-el-runner.md) fija el contrato del
  workspace; pasa a `Aceptado` con L11.
- [ADR-0017](../../../adr/0017-replay-determinista-con-fingerprint.md): el bucle y los
  adaptadores deben ser **deterministas** (lo que se sirve entra en el *prompt* del
  siguiente turno y el *fingerprint* ya lo cubre).
- El presupuesto real es de [017](../../017-presupuesto-por-pr/spec.md); 016 solo define
  el puerto mínimo que consume.
