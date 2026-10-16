Última modificación: 2026-10-16 | Creado: 2026-10-15

# Todo: Fase 3.5 (modelos declarativos por rol)

Checklist de la iniciativa. El diseño está en
`docs/iniciativas/archivo/005-modelos-declarativos/spec.md`; el cómo y el orden, en
`docs/iniciativas/archivo/005-modelos-declarativos/plan.md`; el estado del conjunto, en
`docs/iniciativas/README.md`. Orden por dependencia. Spec aprobada el 2026-10-15.

Decisiones de arranque (2026-10-15): la unidad de asignación es el **rol**, no el
nodo; el registro se lee **al arrancar** (sin recarga en caliente); el `LlmPort`
se **amplía ahora** (`json_mode` + `LlmReply` con `usage`); `correctness` y `fixer`
arrancan con `deepseek-v4-flash`; el presupuesto real por PR es deuda aparte (D17).

## Bloque 1 — Contrato del registro (sin LLM real)

- [x] **M1** `config/models.yaml` y su esquema
  - Fichero versionado con `providers`, `models` y `bindings`; esquema Pydantic
    (`ModelsConfig`) que rechaza un `kind` distinto de `chat`, un binding a un
    modelo inexistente, un `provider` no declarado y una clave desconocida.
    Interpolación de `${VAR}` contra los campos de `Settings` en mayúsculas: una
    referencia sin resolver se nombra en `insecure_secrets()`. `implicit_config`
    sintetiza el registro desde `LLM_MODEL`/`LLM_BASE_URL`/`LLM_API_KEY` cuando
    `MODELS_CONFIG` está vacío (D7).
  - Aceptación: sin `MODELS_CONFIG` el comportamiento es el de hoy; un YAML con
    binding roto falla al validar; `${LLM_API_KEY}` sin valor cuenta como inseguro.
  - Verifica: `tests/test_models_config.py`. ✅ (18 casos)
  - Ficheros: `config/models.yaml`, `src/agent_review/models_config.py`,
    `src/agent_review/settings.py`, `.env.example`.

- [x] **M2** `ModelRegistry`
  - `port_for(rol)` devuelve un `LlmPort` parametrizado y cacheado por modelo (dos
    roles con el mismo modelo comparten instancia); `None` si el rol no está
    bindeado. `probe()` contrasta los bindings contra `GET /v1/models`.
  - Aceptación: dos roles con modelos distintos devuelven puertos distintos; dos
    roles con el mismo modelo, la misma instancia; un rol ausente, `None`.
  - Verifica: `tests/test_models_registry.py`. ✅ (6 casos)
  - Ficheros: `src/agent_review/models_registry.py`.

### Checkpoint: tras M1–M2

- [x] `config/models.yaml` valida y `implicit_config` reproduce el comportamiento
      actual; `make ci` en verde sin tocar `.env`. ✅ (475 tests)

## Bloque 2 — Puerto y adaptador (una sola unidad de cambio)

- [x] **M3** Puerto ampliado
  - `LlmReply` (`text`, `prompt_tokens`, `completion_tokens`, `reasoning_tokens`,
    `finish_reason`); `complete` gana `json_mode` y `response_schema`. Un
    `finish_reason == "length"` con `text` vacío se convierte en `LlmError`
    explícito (un razonador puede devolver `content: null` con 200 OK).
  - Aceptación: el contrato no permite pedir `json_schema` sin `response_schema`;
    el caso `length` vacío levanta `LlmError` con traza.
  - Verifica: cubierto por `tests/test_openai_llm.py` (adaptador). ✅
  - Ficheros: `src/agent_review/ports/llm.py`.

- [x] **M4** Adaptador `openai_llm`
  - Traduce `json_mode` a `response_format` (`json_object` o `json_schema` con el
    esquema); aplica los `defaults` del proveedor (`temperature`,
    `reasoning_effort`); parsea `usage` (tolerante a su ausencia → `0`); devuelve
    `LlmReply`. `reasoning_min_max_tokens` eleva el techo cuando el modelo razona.
  - Aceptación: `json_object` añade la mención a JSON en el prompt; un
    `json_schema` pedido a un modelo que no lo declara **no** se envía; sin
    `usage`, los contadores son `0` y no falla.
  - Verifica: `tests/test_openai_llm.py`. ✅ (15 casos)
  - Ficheros: `src/agent_review/adapters/openai_llm.py`.

- [x] **M5** Consumidores del puerto
  - `LlmReviewer` y `LlmFixer` consumen `LlmReply`: exponen `last_usage` con el
    gasto real del proveedor, además de `last_estimated_tokens` como cota previa.
    Ambos piden `json_mode="json_object"`. Los fakes de test se migraron.
  - Aceptación: `test_llm_reviewer`, `test_llm_fixer`, `test_retry`,
    `test_e2e_tooling` en verde.
  - Verifica: `tests/test_llm_reviewer.py`, `tests/test_llm_fixer.py`,
    `tests/test_retry.py`, `tests/test_e2e_tooling.py`. ✅
  - Ficheros: `src/agent_review/adapters/llm_reviewer.py`,
    `src/agent_review/adapters/llm_fixer.py`.

### Checkpoint: tras M3–M5

- [x] El puerto negocia `json_mode` y devuelve `usage`; `finish_reason=length`
      vacío es `LlmError`; `make ci` en verde con los fakes migrados. ✅

## Bloque 3 — Cableado, fail-fast y cierre

- [x] **M6** Cableado del worker por rol
  - `build_registry` construye el registro; `build_reviewers` crea una persona por
    rol de `LLM_REVIEW_ROLES` (`correctness`, `security`, `arch`) con su modelo;
    `build_fixer` usa `registry.port_for("fixer")`. `build_llm` desaparece.
  - Aceptación: con bindings distintos, cada rol usa su modelo; un rol sin binding
    no construye su persona.
  - Verifica: `tests/test_worker.py` (incluye `test_build_reviewers_bindea_cada_rol_a_su_modelo`). ✅
  - Ficheros: `src/agent_review/worker/main.py`.

- [x] **M7** `probe()` y validación al arrancar
  - `GET /v1/models` al arrancar: un modelo bindeado que el proveedor no lista
    impide arrancar; un proveedor inalcanzable solo se registra como aviso (el
    worker debe poder arrancar sin salida a internet). Test `live` que además
    comprueba las capacidades declaradas.
  - Aceptación: un modelo inventado impide arrancar; el test `live` detecta una
    capacidad mal declarada (verificado: un `json_schema` en `deepseek-v4-flash`
    da 400 y el test falla).
  - Verifica: `tests/test_models_registry.py`, `tests/test_live_models.py` (`-m live`). ✅
  - Ficheros: `src/agent_review/models_registry.py`,
    `src/agent_review/worker/main.py`, `pyproject.toml`.

- [x] **M8** Docs, ADR y cierre
  - **ADR-0015** (registro declarativo de modelos por rol); `operacion.md` §2 y §3,
    `arquitectura.md` (§1–§7 D17), `CONTEXT.md` (rol, registro, binding,
    capacidad), `README.md`, `desarrollo.md` (mapa de tests y cómo añadir un
    proveedor), `.env.example`.
  - Aceptación: `make docs-check` y `make ci` en verde.
  - Verifica: `make docs-check`. ✅
  - Ficheros: `docs/adr/0015-registro-declarativo-de-modelos.md`,
    `docs/adr/README.md`, `docs/operacion.md`, `docs/arquitectura.md`,
    `CONTEXT.md`, `README.md`, `docs/desarrollo.md`, `.env.example`.

### Checkpoint: fin

- [x] `make ci` en verde (475 tests); verificado contra la API real de
      `nan.builders` (bindings, `probe` y `usage`).
- [x] *(Trasladada el 2026-10-16 a [015](../../015-puesta-en-marcha-real/spec.md) como S2.)* Smoke real sobre un PR de `emotivoo-v2` midiendo hallazgos descartados por el
      contrato (Q3) — requiere P6.

## Bloque 4 — Correcciones de la revisión del PR #3

- [x] **M9** `MODELS_CONFIG` vacío por defecto (D7)
  - Con el valor por defecto `config/models.yaml`, un despliegue con solo `LLM_*`
    pasaba en silencio al registro versionado (otro proveedor, otra clave). Ahora
    el registro se activa declarando `MODELS_CONFIG`, como hace `.env.example`.
  - Verifica: `tests/test_worker.py` (`LLM_MODEL` sin `MODELS_CONFIG` usa el
    registro implícito). ✅
  - Ficheros: `src/agent_review/settings.py`, `docs/operacion.md` §2.
- [x] **M10** `Problem.fatal` decide qué problema del `probe` falla el arranque
  - Antes se decidía comparando el texto del mensaje. Un 401/403 de `/models` pasa
    a ser fatal (la clave no vale); un proveedor que no responde sigue siendo aviso.
  - Verifica: `tests/test_models_registry.py`, `tests/test_worker.py`
    (`validate_models`). ✅
  - Ficheros: `src/agent_review/models_registry.py`, `src/agent_review/worker/main.py`.
- [x] **M11** Sin `json_mode` declarado no se envía `response_format`
  - El adaptador mandaba `json_object` cuando la persona lo pedía aunque el modelo
    no lo declarase (incluida la vía implícita de `LLM_*`).
  - Verifica: `tests/test_openai_llm.py`. ✅
  - Ficheros: `src/agent_review/adapters/openai_llm.py`.

### Checkpoint: tras M9–M11

- [x] `make ci` en verde.

## Pendientes humanos o externos

- [x] **P7** *(Trasladada el 2026-10-16 a [015](../../015-puesta-en-marcha-real/spec.md).)* Rotar la API key de `nan.builders` usada en las pruebas (quedó
      expuesta en el chat de diseño) antes de dar por cerrada la iniciativa.
- [x] **Q3 del spec** *(Trasladada el 2026-10-16 a [015](../../015-puesta-en-marcha-real/spec.md) con S2.)* promover un rol a `json_schema` estricto — se mide en el
      smoke real, no antes.

## Notas / límites conocidos

- El presupuesto de tokens sigue siendo **por llamada**, no por PR (deuda D17 de
  `arquitectura.md` §7): se aborda en una iniciativa aparte.
- Sin recarga en caliente: cambiar un binding exige reiniciar el worker.
- La vía `LLM_*` se mantiene como compatibilidad (D7); no tiene fecha de retirada.
