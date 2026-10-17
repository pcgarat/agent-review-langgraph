Última modificación: 2026-10-17 | Creado: 2026-10-16

# Lector de contexto: puerto, bucle de herramientas y workspace de lectura

> **Estado:** implementada *(2026-10-16)*
>
> **Entrega objetivo:** una pieza compartida que permite a un modelo **leer el
> repositorio** mientras razona: el `ContextReaderPort` con tres adaptadores, un
> bucle de herramientas acotado sobre `LlmPort` y un workspace de solo lectura en el
> `sandbox-runner` ([ADR-0018](../../../adr/0018-workspace-de-lectura-en-el-runner.md)).
> La usan el verificador de [009](../../009-verificador-adversarial/spec.md) y el
> revisor de [014](../../014-revisor-con-herramientas/spec.md). No ejecuta código del PR.

---

## 1. Objetivo

Tanto 009 como 014 necesitan lo mismo: que el modelo pida un fichero o haga un `grep`,
que el sistema se lo sirva con límites y que lo que cite se pueda comprobar. Hasta el
2026-10-16 esa pieza estaba especificada dentro de 009 (§5.4 y §5.4.1) y 014 decía
que «la crea la primera que se implemente». Pero la pieza no depende del contrato de
verificación de [007](../007-contrato-de-verificacion/spec.md). Dejarla dentro de 009
obligaba a 014 a esperar toda la cadena 007 → 009.

Se quiere:

1. **Un solo puerto** (`ContextReaderPort`) con tres adaptadores: por VCS, por
   workspace y por fixture.
2. **Un solo bucle** de herramientas (`adapters/tool_loop.py`) con el protocolo de
   turno, los topes y la comprobación de citas. Cada consumidor aporta su *prompt* y
   su objeto final (veredicto o hallazgos).
3. **El workspace de lectura** del runner, que permite hacer `grep` sobre el árbol
   completo sin dar credencial de clonado al worker.

## 2. No-objetivos

- **Qué hace el modelo con lo que lee.** El veredicto es de 009 y los hallazgos son
  de 014. Este trabajo solo sirve el contexto y comprueba las citas.
- **Ejecutar código.** El workspace sirve objetos de git; `/verify` no cambia.
- ***Function calling* nativo.** El bucle es *ReAct* en JSON sobre `LlmPort`
  ([009 §3.3](../../009-verificador-adversarial/spec.md)).
- **El presupuesto por PR.** El bucle consulta un presupuesto que le pasa quien lo
  llama; el presupuesto por PR es [017](../017-presupuesto-por-pr/spec.md).

## 3. Contexto y evidencia

- `VcsPort.get_file(repo, path, *, ref)` ya existe
  ([`ports/vcs.py`](../../../../src/agent_review/ports/vcs.py)), pero lee fichero a
  fichero, cada lectura lanza un proceso `twg` y no permite buscar en el árbol.
- `LlmPort.complete` ([`ports/llm.py`](../../../../src/agent_review/ports/llm.py)) no
  acepta `tools`.
- El `sandbox-runner` ya clona `repo@head_sha` con un token de solo lectura
  (`GitWorkspace`, [`sandbox_runner/workspace.py`](../../../../src/agent_review/sandbox_runner/workspace.py))
  y es el único proceso con esa credencial
  ([ADR-0011](../../../adr/0011-sandbox-runner-aislado.md)).
- El *replay* del *gold set*
  ([ADR-0017](../../../adr/0017-replay-determinista-con-fingerprint.md)) exige que todo
  lo que entra en el *prompt* sea determinista.

## 4. Decisiones

| # | Decisión | Motivo |
|---|---|---|
| W1 | **Un puerto, tres adaptadores:** `VcsContextReader` (por `get_file`), `WorkspaceContextReader` (por el runner) y `FixtureContextReader` (ficheros del caso, para el *replay*) | Los consumidores no saben de dónde sale el contexto; el *replay* es determinista |
| W2 | **Un bucle compartido** (`tool_loop`): protocolo de turno, hasta **3** peticiones por turno, **un** turno correctivo ante JSON inválido y un **turno final forzado** al agotar un tope | Mismas reglas para 009 y 014; las cifras salen de 014 Q2 |
| W3 | El bucle lleva un **registro de lo servido** (`served: dict[path, ref]`) y expone `cite_served(cita)`; los consumidores lo usan para validar citas | Es la comprobación de [009 V4](../../009-verificador-adversarial/spec.md) y [014 H7](../../014-revisor-con-herramientas/spec.md), escrita una sola vez |
| W4 | El resultado de cada herramienta va **delimitado** (`<tool_result>`) y el *system prompt* base dice que es dato, no instrucción | Defensa común contra la inyección desde ficheros del repo |
| W5 | El `grep` sobre el árbol completo lo sirve un **workspace de lectura** en el runner: clon *bare* leído con `git show`, `git grep` y `git ls-tree`, sin *checkout* ni contenedor | [ADR-0018](../../../adr/0018-workspace-de-lectura-en-el-runner.md) |
| W6 | El workspace se abre de forma **idempotente por `(repo, head_sha)`**, caduca por TTL (1800 s) con un máximo de 4 abiertos, y **no viaja en el estado** | Lo comparten 009 y 014 sin coordinar quién lo abre; una pausa HITL de días no deja clones vivos (cifras de 009 Q9) |
| W7 | **Rutas vetadas en el runner** (`context_denylist:` del perfil; default `.env*`, `*.pem`, `*.key`, `**/secrets/**`) | El modelo no puede saltárselo pidiendo otra ruta |
| W8 | **Degradación:** sin runner, o tras un `410` (workspace perdido) y una reapertura fallida, se usa `VcsContextReader`; si el VCS falla, la herramienta devuelve un error al modelo y el bucle sigue. *Enmienda 2026-10-16:* decía `404`, que es también «fichero ausente»; una ruta inventada por el modelo gastaba la única reapertura y la pérdida real ya no degradaba | Una pieza de contexto nunca tumba una review |

## 5. Diseño o contrato

### 5.1 Puerto

```python
@runtime_checkable
class ContextReaderPort(Protocol):
    def read_file(
        self,
        path: str,
        *,
        ref: Literal["head", "base"] = "head",
        start: int | None = None,
        end: int | None = None,
    ) -> str | None: ...
    def grep(self, pattern: str, *, glob: str | None = None) -> list[str]: ...  # `path:line: texto`
    def list_dir(self, path: str) -> list[str]: ...
```

| Adaptador | `read_file` | `grep` | `list_dir` |
|---|---|---|---|
| `VcsContextReader` | `VcsPort.get_file` al `head_sha` o al `base_sha` | Solo sobre lo ya servido y los ficheros del diff | Solo rutas del diff |
| `WorkspaceContextReader` | `GET /workspaces/{id}/file` | Árbol completo a `head_sha` | Árbol completo |
| `FixtureContextReader` | Ficheros del caso del *gold set* | Sobre esos ficheros | Sobre esos ficheros |

### 5.2 Bucle

```python
def run_tool_loop(
    llm: LlmPort,
    *,
    system: str,
    prompt: str,
    reader: ContextReaderPort,
    limits: ToolLimits,          # turnos, peticiones, bytes
    budget: Budget | None,       # lo aporta quien llama (017)
    is_final: Callable[[dict], bool],
) -> LoopResult: ...             # texto final, served, turns, tool_calls, bytes, usage
```

- Cada respuesta del modelo es `{"tools": [...]}` o el objeto final del consumidor
  (lo reconoce `is_final`).
- Las peticiones que pasan de 3 por turno se descartan y se avisa al modelo en el
  resultado.
- Agotado un tope (turnos, peticiones, bytes o presupuesto), el siguiente turno pide
  el objeto final sin herramientas.
- Un error transitorio del modelo sube a quien llama: el reintento es el del nodo
  ([ADR-0007](../../../adr/0007-reintento-en-dos-capas.md), 014 Q5).

### 5.3 Workspace de lectura en el runner

El contrato y las reglas están en
[ADR-0018](../../../adr/0018-workspace-de-lectura-en-el-runner.md). Los endpoints:

| Método | Ruta | Qué hace |
|---|---|---|
| `POST` | `/workspaces` `{repo, head_sha, base_sha}` | Clona *bare* (o reutiliza el clon) y devuelve `{workspace_id, expires_at}`. Idempotente por `(repo, head_sha)` |
| `GET` | `/workspaces/{id}/file?path=&ref=head\|base&start=&end=` | `git show <sha>:<path>`; `404` si no existe, `403` si la ruta está vetada y `410` si el workspace no existe *(enmienda 2026-10-16)* |
| `POST` | `/workspaces/{id}/grep` `{pattern, glob, max_matches}` | `git grep -n` sobre `head_sha`, con timeout y tope de coincidencias |
| `GET` | `/workspaces/{id}/tree?path=` | `git ls-tree` del directorio a `head_sha` |
| `DELETE` | `/workspaces/{id}` | Borra el clon; el TTL lo borra igualmente |

Variables nuevas: `SANDBOX_WORKSPACE_TTL_S` (1800) y `SANDBOX_MAX_WORKSPACES` (4). El
worker de reviews gana `SANDBOX_RUNNER_URL` y `SANDBOX_RUNNER_TOKEN`, solo para
`/workspaces`.

## 6. Riesgos

| # | Riesgo | Mitigación |
|---|---|---|
| R1 | **El runner ejecuta código del PR** (`setup_network`) y ahora también sirve lecturas: comprometido, podría servir contenido falso | El clon *bare* vive fuera de lo que se monta en los contenedores; un runner comprometido ya es un incidente de [ADR-0011](../../../adr/0011-sandbox-runner-aislado.md); los consumidores no tocan la severidad |
| R2 | **Inyección de *prompt*** desde el contenido servido | Delimitación y regla en el *system prompt* (W4); herramientas de solo lectura; veto de rutas en el runner (W7). Lo que cada consumidor hace con un contenido hostil se mide en 009 y 014 |
| R3 | **Disco del runner** con clones grandes | Tope de workspaces (W6), TTL y *reaper*; el clon es *bare* |
| R4 | **`grep` con patrones caros** | `git grep` con timeout y `max_matches`; error al modelo, no a la review |
| R5 | **Pérdida de los workspaces** al reiniciar el runner | Reapertura idempotente una vez y, si falla, degradación (W8) |

## 7. Criterios de hecho

1. Los tres adaptadores cumplen el mismo test de contrato del puerto.
2. El bucle respeta cada tope y hace el turno final forzado (un test por tope); un
   JSON inválido recibe un turno correctivo y después se cierra.
3. `cite_served` rechaza una cita a un fichero no servido (test).
4. El runner: una ruta vetada da `403` y no aparece en el `grep`; abrir dos veces el
   mismo `(repo, head_sha)` devuelve el mismo workspace; un workspace caducado lo
   borra el *reaper* (tests del runner).
5. Con el runner caído, el lector se degrada a `VcsContextReader` (test).
6. `make ci` en verde, con `make docs-check`; documentos vivos al día:
   [`arquitectura.md`](../../../arquitectura.md) §5 (workspaces del runner),
   [`operacion.md`](../../../operacion.md) §2 (variables) y §6 (endpoints
   `/workspaces`), [`desarrollo.md`](../../../desarrollo.md) §3 (puerto y adaptadores)
   y `CONTEXT.md` («lector de contexto», «workspace de lectura», «bucle de
   herramientas»). ADR-0018 pasa a `Aceptado`.

## 8. Preguntas abiertas

- **Q1 — ¿`git grep` con expresiones regulares o con texto literal por defecto?**
  *Resuelta (2026-10-16):* **literal** por defecto (`git grep -F`; búsqueda literal en
  los adaptadores en memoria). Es más barato y predecible y cierra de raíz el riesgo
  R4 (un patrón regex caro). El modelo rara vez necesita regex para localizar un
  identificador; si lo necesita, dos `grep` literales bastan.
- **Q2 — ¿El `FixtureContextReader` vive en `tests/eval/` o en `src/`?**
  *Resuelta (2026-10-16):* en **`src/agent_review/adapters/`**. El *replay* de
  [ADR-0017](../../../adr/0017-replay-determinista-con-fingerprint.md) lo usa en rutas de
  producto (el bucle de 014 con un rol activado) y `src/` no debe importar de
  `tests/`; además sirve para reproducir una review real a mano con un directorio de
  ficheros.
