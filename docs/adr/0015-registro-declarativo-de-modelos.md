# ADR-0015 — Registro declarativo de modelos por rol

- **Estado:** Aceptado
- **Fecha:** 2026-10-15

## Contexto

El LLM detrás del puerto `LlmPort` estaba cableado con tres ajustes planos
(`LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY`) y un booleano. Un único modelo servía
a todas las personas y al fixer. Con dos o tres personas en el fan-out y un fixer
que propone parches —tareas con coste, contexto y formato de salida distintos— esa
configuración impide lo obvio: un modelo grande para el fixer y uno barato para la
revisión, o `json_schema` estricto solo donde compensa.

A la vez, el proveedor elegido (`nan.builders`) **no expone capacidades** en
`/v1/models` y **rechaza con 400 permanente** un `json_schema` pedido a un modelo
que no lo soporta. Un error permanente no lo recupera la `RetryPolicy`: la persona
degradaría a 0 hallazgos **en cada review**, en silencio.

## Decisión

- **La unidad de asignación es el rol, no el nodo.** `correctness`, `security`,
  `arch` y `fixer` son roles; cada uno puede apuntar a un modelo distinto. El nodo
  `persona` es un fan-out con un closure compartido, así que "modelo por nodo"
  forzaría un único modelo para todas las personas.
- **Registro versionado `config/models.yaml`**, leído **al arrancar** (sin recarga
  en caliente, coherente con `autonomy.yaml`). Declara `providers`, `models` (con
  sus capacidades: `json_mode`, `context_window`, `reasoning_min_max_tokens`) y
  `bindings`.
- **Secretos solo por referencia.** El YAML lleva `${LLM_API_KEY}`; el valor vive
  en el entorno. Se integra en `insecure_secrets()`.
- **El puerto negocia capacidades y devuelve `usage` real.** `complete` acepta
  `json_mode`/`response_schema` y devuelve `LlmReply` con `prompt_tokens`,
  `completion_tokens` y `reasoning_tokens`. El adaptador pide al modelo solo lo
  que su especificación declara.
- **Fail-fast al arrancar.** El esquema del YAML, la interpolación de secretos y el
  contraste contra `/v1/models` ocurren antes del primer job.
- **Compatibilidad hacia atrás.** Sin registro declarativo, se sintetiza uno
  implícito desde `LLM_*`; el `.env` y el compose actuales siguen válidos.

## Consecuencias

- Cambiar de modelo es editar un YAML y reiniciar; entrar en un proveedor nuevo es
  añadir un `providers` y un campo de secreto en `Settings`.
- Un modelo de razonamiento que devuelve `content: null` con `finish_reason:
  length` deja de ser un 0 hallazgos silencioso: es un `LlmError` trazable.
- El coste ya no depende de estimar `len(text) // 4`: el `usage` del proveedor es
  la fuente de verdad, y `reasoning_tokens` hace visible el gasto que no aparece en
  el texto.
- El registro es más superficie de configuración: un binding roto falla el arranque
  (deseado) y la vía `LLM_*` queda como deuda de compatibilidad.
- **El presupuesto de tokens sigue siendo por llamada**, no por PR (deuda D17 de
  `docs/arquitectura.md` §7). No lo resuelve este ADR.

## Alternativas descartadas

- **Más variables de entorno** (`LLM_CORRECTNESS_MODEL`, `LLM_FIXER_MODEL`,
  `LLM_SECOND_BASE_URL`...). Degenera en N×M variables y no puede expresar
  capacidades ni contexto.
- **Enrutado dinámico por contenido** (elegir modelo según el diff). Añade
  variabilidad no reproducible y hace el coste impredecible; el binding estático
  por rol cubre el caso real.
- **Recarga en caliente del registro.** Complica la coherencia (un grafo ya
  compilado con un puerto antiguo) sin resolver un problema que hoy no existe.
- **Confiar en `/v1/models` para las capacidades.** El endpoint no las publica; por
  eso se declaran y se comprueban con un test `live`.
