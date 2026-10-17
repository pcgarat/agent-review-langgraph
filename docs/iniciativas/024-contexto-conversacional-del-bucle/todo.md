Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: Contexto conversacional del bucle

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). **El bloque S no toca el *prompt*** y por eso puede hacerse
en paralelo con 023; **A no se empieza sin K3 de 023**.

## Bloque S — Medida del caché (checkpoint S-K)

- [ ] **S1** `cached_tokens` en el puerto
  - `ports/llm.py`: `LlmReply.cached_tokens: int | None = None`; `None` = «el
    proveedor no lo reporta», **distinto de 0** (E3 de la spec). Ajustar el
    docstring: `total_tokens` sigue siendo `prompt + completion`.
  - `ports/llm.py`: `LoopResult` (o su sucesor) separa `prompt_tokens`,
    `completion_tokens`, `reasoning_tokens` y `cached_tokens` además de `usage`
    (E9). `usage` se conserva para no romper el presupuesto.
  - Verifica: `tests/test_llm_reviewer.py` (un `usage` con detalle da número; sin
    él, `None`).
  - Ficheros: `ports/llm.py`, `adapters/tool_loop.py`.

- [ ] **S2** Normalización en el adaptador
  - `adapters/openai_llm.py`: `_read_message` y `_read_stream` devuelven el `usage`
    completo; una sola función `_cached_tokens(usage)` lee, por orden,
    `prompt_tokens_details.cached_tokens` (OpenAI-compatible),
    `prompt_cache_hit_tokens` (DeepSeek) y `cache_read_input_tokens` (Anthropic), y
    devuelve `None` si no hay ninguno.
  - **No se inventa un 0**: un campo ausente es «no medido».
  - Verifica: `tests/test_openai_llm.py` con un doble por cada forma de `usage`
    (las tres con caché y una sin ningún campo).
  - Ficheros: `adapters/openai_llm.py`.

- [ ] **S3** Ida y vuelta en la grabación
  - `tests/eval/harness.py`: `record_call` guarda `cached_tokens`; `ReplayLlm` lo
    lee y lo devuelve en el `LlmReply` (`:695` y `:659`). **Los dos lados en el
    mismo cambio**: escribir sin leer devuelve `None` y el informe miente.
  - Las grabaciones existentes no tienen el campo ⇒ `None` (no 0), sin tocar sus
    *fingerprints*.
  - Verifica: `tests/eval/test_eval_harness.py`, test de ida y vuelta con y sin el
    campo.
  - Ficheros: `tests/eval/harness.py`.

- [ ] **S4** Columna en el informe de 006
  - `tests/eval/harness.py`: `cached_tokens / prompt_tokens` por lote y por review;
    `no medido` cuando es `None`. No entra en *recall* ni en precisión: es coste.
  - Verifica: `tests/eval/test_eval_harness.py` (informe con `no medido` y con
    número).
  - Ficheros: `tests/eval/harness.py`.

- [ ] **S5** Prueba de no invalidación
  - Test que calcula el *fingerprint* de un prompt conocido antes y después de S
    (no debe cambiar: S no toca `system`, `prompt`, `max_tokens` ni contrato).
  - Aceptación: la grabación actual de `pr_3` sigue casando llamada a llamada
    **después** de S; si algo cambia, es un bug de S, no una regrabación.
  - Verifica: `tests/eval/test_eval_harness.py`.

### Checkpoint S-K

- [ ] `cached_tokens` llega del adaptador al informe; `None` ⇒ `no medido`.
- [ ] Ningún *fingerprint* cambia: la grabación de `pr_3` sigue válida.
- [ ] `make ci` en verde.

## Bloque LB — Corrida *live* base, compartida con 023 (checkpoint LB-K)

- [ ] **LB1** Una sola corrida de pago para las dos iniciativas
  - Requisito: 023 F1 hecho (el *prompt* de capacidades y **una única** grabación
    de referencia). Esa corrida sirve a la vez como base de 023 y como medida de
    caché de 024; no se apoya en que el *prompt* sea el de hoy, porque 023 E11 lo
    descartó.
  - `LLM_REVIEWER_ENABLED=true REVIEW_TOOLS_ROLES=correctness make eval-record-tools`
    (o el objetivo que fije 023) con la instrumentación de S en su sitio.
  - Anotar en **este** `todo.md` y en el `todo.md` de 023: *prompt*, *completion*,
    razonamiento y `cached_tokens / prompt_tokens`.
  - Aceptación: el informe trae el número real de caché del caso; si el proveedor no
    lo reporta, queda `no medido` y se declara aquí (Q4 de la spec).
  - *Humano / medida:* la decisión de si A se justifica por coste se toma con este
    número.
  - Verifica: `make eval-tools`, informe anotado.

### Checkpoint LB-K

- [ ] El número de caché del caso está anotado aquí y en 023.
- [ ] El *recall* de esa corrida es el *baseline* de 023 con flags en default.
- [ ] Queda escrito si el caché existe, no existe o no se puede medir.

## Bloque A — Puerto con mensajes (checkpoint A-K)

**No se empieza sin K3 de 023** (E14): mover el puerto invalida la grabación y
rompería la comparabilidad del informe por flag.

- [ ] **A1** ADR del puerto conversacional
  - ADR nuevo con el **siguiente número libre cuando se escriba**, no ahora (regla
    `documentacion` §2: el número se asigna al crear el fichero). Previsto ~0027
    por el orden de 023 (su T1 va antes); si al llegar no está libre, se usa el que
    toque sin tocar este documento. Alternativas: `messages` frente a string con
    marcas; una API frente a dos; rol `tool` frente a `<tool_result>` en `user`.
  - Enmienda con fecha lo que [ADR-0017](../../adr/0017-replay-determinista-con-fingerprint.md)
    dice del *prompt* como string.
  - Ficheros: `docs/adr/`, `docs/adr/README.md`.

- [ ] **A2** `LlmMessage` y `as_messages`
  - `ports/llm.py`: `LlmMessage(role, content)` con `Literal["system", "user",
    "assistant"]`; `as_messages(system, prompt)` como función pura.
  - `ports/llm.py`: `complete(*, messages, max_tokens, ...)`; se retiran `system` y
    `prompt` del puerto (no se mantienen dos formas).
  - Verifica: test de contrato del puerto; `mypy` en verde.
  - Ficheros: `ports/llm.py`.

- [ ] **A3** Llamantes
  - `adapters/openai_llm.py`: construye `payload["messages"]` desde la lista.
  - `adapters/llm_reviewer.py`, `adapters/llm_fixer.py`, `adapters/llm_verifier.py`
    y los dobles de test: usan `as_messages` mientras no tengan conversación propia.
  - Verifica: `tests/test_llm_reviewer.py`, `tests/test_llm_fixer.py`,
    `tests/test_llm_verifier.py`, `tests/test_openai_llm.py`.
  - Ficheros: adaptadores y tests.

- [ ] **A4** El bucle acumula en vez de reconstruir
  - `adapters/tool_loop.py`: `messages = [system, user]`; en cada turno se **añade**
    el `assistant` con el texto **crudo** (nunca `json.dumps`) y un `user` con el
    bloque de `<tool_result>`.
  - `_FORCE_FINAL` y `_CORRECTIVE`: **un** mensaje cada uno, en su turno, sin
    acumularse; `_CORRECTIVE` deja de persistir como hoy.
  - `_estimate_tokens` suma los `content` de los mensajes (y declara que puede
    subir por el eco: R11).
  - Test de prefijo: `messages(turno N+1)[:len(turno N)] == messages(turno N)` byte
    a byte, con estado de lecturas y avisos de 023 al final del último `user`.
  - Verifica: `tests/test_tool_loop.py`.
  - Ficheros: `adapters/tool_loop.py`, `ports/context.py` si el tope entra ahí.

- [ ] **A5** `fingerprint` v3
  - `tests/eval/harness.py`: hash sobre rol + lista ordenada de **estructuras**
    (`role`, `content` por mensaje) + `max_tokens` + contrato de salida. **No**
    hashear la concatenación: `["ab","c"]` y `["a","bc"]` colisionarían.
  - Una grabación v2 **aborta** con `FingerprintMismatch` (depende de 023 T0: sin él
    el fallo se lee como recall 0).
  - Verifica: `tests/eval/test_eval_harness.py` (v2 aborta; dos listas de igual
    concatenación y distinta estructura **no** colisionan).
  - Ficheros: `tests/eval/harness.py`.

- [ ] **A6** Regrabación del caso
  - Regrabar `pr_3` con 023 en su default y la estructura nueva; el diff de
    `recorded_tools.json` es revisable (ADR-0017).
  - Aceptación: el *replay* corre sin `FingerprintMismatch`; ninguna llamada sobra
    ni falta.
  - Verifica: `make eval-tools`.
  - Ficheros: `tests/eval/pr_3/recorded_tools.json`.

### Checkpoint A-K

- [ ] El puerto solo acepta `messages`; ningún llamante pasa strings sueltos.
- [ ] El prefijo es byte a byte estable entre turnos, con avisos y estado al final.
- [ ] `_FORCE_FINAL`/`_CORRECTIVE` no se acumulan.
- [ ] El *fingerprint* v3 rechaza v2 y no colisiona por concatenación.
- [ ] `make ci` en verde.

## Bloque R — Informe antes/después y decisión sobre B (checkpoint R-K)

- [ ] **R1** Informe comparado
  - Sobre las dos grabaciones (la de LB y la de A6): *prompt*, *completion*,
    razonamiento, `cached_tokens / prompt_tokens` y recall.
  - Declarar el delta del eco del *assistant*: si **sube** el *prompt*, se dice (E7
    de la spec; no se presenta como ahorro).
  - Anotar el número aquí o en la spec.
  - Verifica: `make eval-tools`, informe anotado.

- [ ] **B1** Decisión escrita sobre B
  - Con el informe delante: o se abre iniciativa propia para
    `plan`/`read`/`review`, o se cierra 024 sin ella. La regla de §5.6 de la spec
    exige **≥2 casos** de *gold set* y matcher estable (023 K0).
  - Aceptación: la decisión y su número están en la spec; no se deja «pendiente de
    valorar».
  - *Humano / medida:* la decisión es humana; el número lo pone R1.

### Checkpoint R-K

- [ ] Informe antes/después con caché y razonamiento anotado.
- [ ] Decisión sobre B escrita con su número.

## Bloque D — Docs y cierre (checkpoint D-K)

- [ ] **D1** Documentos vivos
  - `arquitectura.md` §3–§4: el bucle mantiene mensajes; §7: D26 y D27 resueltas.
  - `desarrollo.md` §3: el puerto con mensajes y `as_messages`.
  - `CONTEXT.md`: «mensaje», «prefijo cacheable», «tokens cacheados»; «no medido»
    frente a 0.
  - `operacion.md` §2 solo si aparece variable nueva (no debería: E11 de la spec).
  - Spec → `implementada`; fila de 024 en `docs/iniciativas/README.md`.
  - Aceptación: `make ci` y `make docs-check` en verde; criterios 1–10 de la spec
    verificados uno a uno.

### Checkpoint D-K

- [ ] Docs al día; `make ci` y `make docs-check` en verde; iniciativa cerrable.
