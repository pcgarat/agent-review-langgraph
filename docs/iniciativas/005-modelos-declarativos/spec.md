Última modificación: 2026-10-16 | Creado: 2026-10-15

# Fase 3.5 — Modelos declarativos por rol

Registro declarativo de proveedores y modelos, con asignación **por rol** (no por
nodo de grafo), y extensión del `LlmPort` para negociar capacidades y contabilizar
el gasto real. Primer proveedor: `nan.builders`.

> **Estado:** implementada (2026-10-15)
> **Entrega objetivo:** arrancar con `correctness` y `fixer` apuntando a modelos
> distintos, declarados en `config/models.yaml`, y que un modelo sin soporte de
> esquema o sin `reasoning_effort` no falle en la primera review.
>
> **Enmienda (2026-10-15):** Q1 se resuelve al aprobar la spec —`correctness` y
> `fixer` usan `deepseek-v4-flash`—, con revisión tras el smoke real. El
> presupuesto de tokens (deuda D17) se aborda en una iniciativa aparte, no aquí.
> El secreto del proveedor se referencia como `${LLM_API_KEY}`: el registro solo
> resuelve referencias contra campos de `Settings` (un proveedor nuevo añade el
> suyo), y el test `live` comprueba las capacidades contra la API.

---

## 1. Objetivo

Hoy el LLM está detrás de un puerto (`LlmPort`), pero el cableado asume **un solo
modelo para todo**: tres settings planos (`LLM_MODEL`, `LLM_BASE_URL`,
`LLM_API_KEY`) y un booleano (`LLM_REVIEWER_ENABLED`) que `build_reviewers` y
`build_fixer` consumen sin discriminar.

Se quiere:

1. **Varios proveedores y varios modelos** en el mismo despliegue.
2. **Asignación por rol**: `correctness` puede usar un modelo barato de 1M de
   contexto y `fixer` otro con esquema estricto.
3. **Cambio declarativo**: editar un YAML versionado, no código ni un puñado de
   variables de entorno, y reiniciar.
4. **Fail-fast**: un binding roto, un secreto sin resolver o un modelo inexistente
   se detectan **al arrancar**, no en la primera review.
5. **Contabilidad honesta**: usar el `usage` real del proveedor en vez de estimar
   `len(text) // 4`.

## 2. No-objetivos

- **Recarga en caliente.** El registro se lee al arrancar; cambiar de modelo
  requiere reiniciar el worker (decisión D2). Coherente con `autonomy.yaml`.
- **Enrutado dinámico por contenido** (elegir modelo según el diff). El binding es
  estático por rol.
- **Modelos no-chat** (`embedding`, `rerank`, `tts`, `stt`). El esquema los declara
  para poder detectarlos y rechazarlos, pero no se integran.
- **Sustituir `PROVIDER`/`settings` de VCS o de sandbox.** Solo se toca el plano LLM.
- **Arreglar el presupuesto de tokens.** Se documenta como deuda (§6, D8) y se
  resuelve en una iniciativa aparte.

## 3. Contexto y evidencia

### 3.1 El proveedor `nan.builders`

Contrato OpenAI-compatible: `base_url https://api.nan.builders/v1`,
`Authorization: Bearer <key>`. Modelos de la cuenta, comprobados contra
`GET /v1/models`:

| Modelo | Contexto | Cuota | JSON mode | `json_schema` | Reasoning |
|---|---|---|---|---|---|
| `deepseek-v4-flash` | 1M | 3B tok/mes | `json_object` (exige la palabra "JSON" en el prompt) | ❌ 400 | controlable |
| `qwen3.6` | 262K | — | ✅ | ✅ | **on por defecto** |
| `gemma4` | 262K | — | ✅ | ✅ | controlable |
| `glm5.3-flash` | 1M | 2B tok/mes | — | — | siempre razona |
| `qwen3.8-flash` | 1M | 500M tok/mes | — | — | on por defecto |
| `mimo-v2.6-flash` | 1M | 1B tok/mes | — | — | controlable |
| `glm5.3` | 1M | 3B tok/mes | — | — | premium, aparte |

### 3.2 Comportamientos verificados (no teóricos)

Tres sondas contra la API real que condicionan el diseño:

1. **`json_schema` es un 400 permanente en `deepseek-v4-flash`.** El propio error
   remite a `qwen3.6`/`gemma4`. Un error permanente **no** lo recupera la
   `RetryPolicy` del nodo, así que pedir esquema al modelo equivocado degrada la
   persona a 0 hallazgos en cada review.
2. **Un modelo de razonamiento puede devolver `content: null` sin error.**
   `qwen3.6` con `max_tokens: 64` gastó todo el presupuesto en
   `reasoning_content` y devolvió `finish_reason: "length"` con `content: null`.
   Hoy eso entra en `_parse_json("")` y produce **0 hallazgos silenciosos**: la
   review se publica como limpia sin haber revisado nada.
3. **`usage` real disponible.** Con `reasoning_effort: "none"` +
   `response_format: {"type": "json_object"}`, `deepseek-v4-flash` devuelve
   `prompt_tokens`, `completion_tokens` y `reasoning_tokens` utilizables.

## 4. Decisiones

- **D1 — La unidad de asignación es el rol, no el nodo.** El nodo `persona` es un
  fan-out: todas las ramas comparten el mismo closure, así que "modelo por nodo"
  forzaría a que `correctness`, `security` y `arch` usaran el mismo modelo. La
  unidad semántica es el rol (persona o fixer).
- **D2 — Declarativo, leído al arrancar.** `config/models.yaml` versionado, sin
  recarga en caliente. Coherente con `config/autonomy.yaml`.
- **D3 — Registro explícito de proveedores y modelos.** El YAML declara cada
  modelo con sus capacidades; los roles se bindean a modelos. El código no conoce
  ningún nombre de modelo.
- **D4 — Secretos solo por referencia.** El YAML lleva `${NAN_API_KEY}`; el valor
  vive en `.env`/gestor de secretos. Se integra en `insecure_secrets()`.
- **D5 — El puerto negocia capacidades y devuelve `usage` real.** Se amplía
  `LlmPort` ahora, mientras solo hay dos consumidores y ~4 fakes.
- **D6 — Fail-fast al arrancar.** Esquema del YAML, interpolación de secretos y
  contraste contra `GET /v1/models`, todo antes de procesar el primer job.
- **D7 — Compatibilidad hacia atrás.** Sin `MODELS_CONFIG`, se sintetiza un
  registro implícito desde `LLM_MODEL`/`LLM_BASE_URL`/`LLM_API_KEY`. El despliegue
  actual y los tests siguen funcionando sin tocar `.env`.

## 5. Diseño o contrato

### 5.1 `config/models.yaml`

```yaml
version: 1

providers:
  nan:
    protocol: openai
    base_url: https://api.nan.builders/v1
    api_key: ${NAN_API_KEY}
    timeout_s: 60
    defaults:
      temperature: 0
      max_tokens: 8192
      reasoning_effort: none

models:
  deepseek-v4-flash:
    provider: nan
    kind: chat
    context_window: 1048576
    json_mode: json_object
    min_max_tokens_with_reasoning: 16384
  qwen3.6:
    provider: nan
    kind: chat
    context_window: 262144
    json_mode: json_schema
  gemma4:
    provider: nan
    kind: chat
    context_window: 262144
    json_mode: json_schema

bindings:
  correctness: deepseek-v4-flash
  fixer: deepseek-v4-flash
```

### 5.2 `ModelRegistry`

```python
class ModelRegistry:
    @classmethod
    def from_file(cls, path: str, env: Mapping[str, str]) -> "ModelRegistry": ...
    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelRegistry": ...  # D7
    def port_for(self, role: str) -> LlmPort | None: ...
    def probe(self) -> list[Problem]: ...
```

Cachea un `LlmPort` por modelo: dos roles con el mismo modelo comparten instancia.
`port_for` devuelve `None` si el rol no está bindeado (la persona no se construye,
igual que hoy).

### 5.3 Puerto ampliado

```python
@dataclass(frozen=True)
class LlmReply:
    text: str
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    finish_reason: str

class LlmPort(Protocol):
    def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        json_mode: Literal["json_object", "json_schema"] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> LlmReply: ...
```

`finish_reason == "length"` con `text` vacío se convierte en `LlmError`
explícito: deja traza y no fabrica una review limpia.

### 5.4 Cableado

`build_reviewers` y `build_fixer` resuelven por rol:

```python
llm = registry.port_for("correctness")   # persona
llm = registry.port_for("fixer")         # fixer
```

## 6. Riesgos

| # | Riesgo | Mitigación |
|---|---|---|
| R1 | El `usage` real de `nan` no sigue el estándar OpenAI (campos distintos) | La sonda §3.2 ya confirmó los tres campos; el adaptador tolera su ausencia con `0` |
| R2 | Un modelo de razonamiento agota el presupuesto en `reasoning_tokens` y devuelve vacío | `reasoning_effort: none` por defecto + `LlmError` en `finish_reason == length` |
| R3 | Las capacidades declaradas en el YAML no se verifican solas | Test `live` que las comprueba contra la API; `/v1/models` no las expone |
| R4 | Romper el Protocol del puerto deja sin actualizar algún fake | `mypy strict` ya cubre `src`; los fakes de `tests` se ajustan en la misma tarea |
| R5 | El registro implícito (D7) queda como deuda silenciosa | Se documenta en `operacion.md` §2 como vía de compatibilidad, con fecha de retirada a decidir |
| **D8** | **El presupuesto de tokens es por llamada, no por PR** (deuda preexistente) | Documentado en `arquitectura.md` §7; se arregla aparte |

## 7. Criterios de hecho

- [ ] `config/models.yaml` valida con Pydantic; un `kind` distinto de `chat` o un
      binding a un modelo inexistente falla al arrancar.
- [ ] `${NAN_API_KEY}` sin resolver cuenta como secreto inseguro en
      `insecure_secrets()` (producción sigue fail-closed).
- [ ] `GET /v1/models` se contrasta al arrancar; un modelo no listado falla.
- [ ] `correctness` y `fixer` pueden apuntar a modelos distintos.
- [ ] Con `MODELS_CONFIG` vacío, el comportamiento actual no cambia (D7).
- [ ] El `usage` real se expone; la estimación `len//4` deja de ser la única fuente.
- [ ] `finish_reason == "length"` con contenido vacío produce `LlmError` trazable.
- [ ] `make ci` en verde, con `make docs-check`.
- [ ] Test `live` que comprueba las capacidades declaradas contra la API.

> **Enmienda (2026-10-16, revisión del PR #3).** El código se había desviado de
> D7: `MODELS_CONFIG` tenía `config/models.yaml` por defecto, de modo que un
> despliegue con solo `LLM_*` usaba el registro versionado sin tocar su `.env`.
> Ahora el valor por defecto es vacío y `.env.example` declara el registro. En el
> contraste de D6, una credencial rechazada (401/403) por `GET /v1/models` también
> falla el arranque: no es la red, es configuración rota. Un proveedor que no
> responde sigue siendo solo un aviso. Además, el adaptador no envía
> `response_format` a un modelo sin `json_mode` declarado, aunque la persona lo
> pida (D5).

## 8. Preguntas abiertas

- **Q1 — ¿Qué rol usa cada binding por defecto?** Se propone `deepseek-v4-flash`
  para `correctness` y `fixer` (1M de contexto, 3B tok/mes, sin `json_schema` pero
  suficiente para el contrato actual). Confirmado en el arranque de la iniciativa;
  se revisa con el primer smoke real sobre un PR de `emotivoo-v2`.
- **Q2 — ¿Se retira la vía `LLM_*`?** Se mantiene mientras no haya un segundo
  proveedor en uso. Sin fecha de retirada por ahora.
- **Q3 — ¿Se promueve algún proveedor a `json_schema` estricto?** Depende de cuántos
  hallazgos descarte el contrato. Se mide en el smoke, no antes.
