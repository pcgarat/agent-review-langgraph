Última modificación: 2026-10-15 | Creado: 2026-10-15

# Plan: Fase 3.5 (modelos declarativos por rol)

Registro declarativo de proveedores y modelos, con asignación **por rol**. El qué y
el porqué están en `docs/iniciativas/005-modelos-declarativos/spec.md`; las tareas en
`docs/iniciativas/005-modelos-declarativos/todo.md`.

## Overview

Hoy un solo modelo sirve a todas las personas y al fixer, cableado con tres
settings planos. Se sustituye por un **registro declarativo** (`config/models.yaml`)
que bindea cada **rol** a un modelo, y se amplía el `LlmPort` para que negocie
capacidades (`json_mode`) y devuelva el `usage` real del proveedor.

El puerto y el grafo **no cambian de forma**: `persona` y `fix_dispatch` siguen
recibiendo un `LlmPort`. Lo que cambia es de dónde sale y qué sabe hacer.

## Decisiones de arquitectura

- **La unidad es el rol, no el nodo.** `persona` es un fan-out con un closure
  compartido; "modelo por nodo" forzaría un único modelo para todas las personas.
  El rol (`correctness`, `security`, `fixer`) es la unidad semántica (D1).
- **Declarativo, leído al arrancar.** `config/models.yaml` versionado junto a
  `autonomy.yaml`. Sin recarga en caliente (D2): reiniciar el worker es aceptable
  y coherente con la política.
- **Capacidades en el registro, no en el código.** El YAML declara `json_mode`,
  `context_window` y `min_max_tokens_with_reasoning`; el adaptador pide solo lo
  que el modelo declara. Evita el 400 permanente de `json_schema` en
  `deepseek-v4-flash`.
- **Secretos solo por referencia.** `${NAN_API_KEY}` se interpola desde el entorno;
  el valor nunca entra al YAML. Se integra en `insecure_secrets()` (D4).
- **Compatibilidad hacia atrás.** Sin `MODELS_CONFIG`, `ModelRegistry.from_settings`
  sintetiza el registro de un solo modelo desde `LLM_*` (D7). El `.env`, el compose
  y los tests actuales siguen intactos.
- **`finish_reason == "length"` con contenido vacío es un error, no una review
  limpia.** Un modelo de razonamiento puede devolver `content: null` con 200 OK;
  tratarlo como 0 hallazgos silenciosos publicaría una review que no revisó nada.

## Dependency graph

```
M1  contrato del registro (models.yaml + Pydantic + interpolacion + from_settings)
    └── M2  ModelRegistry (port_for(rol), cache por modelo)
             ├── M3  puerto ampliado (json_mode, LlmReply con usage)
             │        ├── M4  adaptador openai_llm (payload, usage, finish_reason)
             │        └── M5  consumidores (llm_reviewer, llm_fixer)
             ├── M6  cableado del worker (build_reviewers/build_fixer)
             └── M7  probe() + validacion en build_runtime + test live
                      └── M8  docs, ADR-0015 y cierre
```

M3–M5 son una sola unidad de cambio (romper el Protocol exige actualizar
adaptador y consumidores a la vez para que `mypy strict` y los tests pasen).

## Task list

Ver `docs/iniciativas/005-modelos-declarativos/todo.md` (M1–M11). Orden por
dependencia: el contrato primero, el puerto en bloque, y el cableado y el `probe`
después. M9–M11 cierran los hallazgos de la revisión del PR #3.

## Checkpoints

- **Tras M1–M2:** `config/models.yaml` valida; `from_settings` reproduce el
  comportamiento actual; `port_for` devuelve el puerto del modelo bindeado.
  `make ci` en verde sin tocar `.env`.
- **Tras M3–M5:** el puerto negocia `json_mode` y devuelve `LlmReply` con `usage`;
  un `finish_reason=length` vacío es `LlmError` trazable. Fakes de test migrados.
- **Tras M6–M7:** `correctness` y `fixer` pueden apuntar a modelos distintos;
  un modelo no listado en `/v1/models` falla al arrancar; secreto sin resolver
  cuenta como inseguro.
- **Tras M8:** `make ci` en verde con `make docs-check`; ADR-0015 y
  `operacion.md` §2 al día.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| El `usage` de `nan` no sigue el estándar OpenAI | Sonda previa confirmó los tres campos; el adaptador tolera su ausencia con `0` (R1) |
| Un razonador agota el presupuesto y devuelve vacío | `reasoning_effort: none` por defecto + `LlmError` en `finish_reason == length` (R2) |
| Las capacidades del YAML no se verifican solas | Test `live` de M7 las comprueba contra la API (R3) |
| Romper el Protocol deja un fake sin actualizar | `mypy strict` cubre `src`; los fakes se migran en M3–M5 (R4) |
| La vía `LLM_*` queda como deuda silenciosa | Documentada en `operacion.md` §2 como compatibilidad, sin fecha de retirada (R5) |
| El presupuesto sigue siendo por llamada | Fuera de alcance: deuda D17 de `arquitectura.md` §7, iniciativa aparte |

## Dependencia externa

- **Q3 del spec** (promover un rol a `json_schema` estricto) se decide con la
  primera medición real de hallazgos descartados por el contrato, no antes.
