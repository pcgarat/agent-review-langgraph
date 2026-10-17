Última modificación: 2026-10-17 | Creado: 2026-10-17

# Contrato de secretos declarado en el modelo

> **Estado:** implementada *(2026-10-17, con la enmienda de abajo)*
>
> **Motivo inmediato:** el refactor `is_missing` (commit `92dea1e`) se dejó cuatro
> comprobaciones con el predicado viejo: `TOOLING_WEBHOOK_SECRET` y
> `REVIEWS_DECISION_TOKEN` vacíos no fallaban en `production`, y `SANDBOX_RUNNER_TOKEN`
> se juzgaba distinto en el worker y en el fix-worker. Corregido en `9fe30b0`, pero el
> patrón que lo causó sigue ahí.
>
> **Decisión:** [ADR-0022](../../adr/0022-contrato-de-secretos-declarado.md).

---

## Enmienda (2026-10-17): lo que el código contradice de este diseño

Al aprobar la spec se contrastaron sus decisiones con el código de hoy y con la
matriz real del contrato (los cuatro procesos × sus condiciones). Dos decisiones no
se sostienen; las tres preguntas abiertas quedan resueltas. El diseño vigente es
**este apartado más §5**, no lo que digan §4 y §8 donde divergan.

- **El runner ya importa `settings`.** `sandbox_runner/main.py:29` hace
  `from agent_review.settings import Settings, get_settings, is_missing` para
  `_configured` (línea 311), y su `Dockerfile` es el mismo (`COPY src ./src`). Es
  decir: **no hay caja que aísle del paquete** y D7/§5.2/R3 (replicar la constante con
  un test de coincidencia) resuelven un problema inexistente. El runner **importa**
  `PLACEHOLDER_VALUES`. Esto hace la entrega más barata: un solo sitio, sin duplicado
  que pueda divergir, que es justo lo que persigue el criterio 1.

- **`required_in` no basta: el contrato tiene dos ejes.** Declarar solo *en qué
  procesos* se exige un secreto pierde casos reales medidos hoy:

  | Secreto | Regla real | Lo que `required_in` solo no dice |
  |---|---|---|
  | `SANDBOX_RUNNER_TOKEN` | `worker`/`api`: si `sandbox_enabled`. `fix-worker`: **siempre** | El proceso no basta: falta la condición, y la condición **difiere por proceso** |
  | `PANEL_SESSION_SECRET` | `web`: **siempre**. `api`/`worker`: si `panel_enabled` | Ídem |
  | `SANDBOX_GIT_TOKEN` | `api`/`worker`: si `sandbox_enabled` | Ídem |
  | `GITHUB_TOKEN` / `TWG_BBC_TOKEN` | uno de los dos, según `VCS_PROVIDER` | Es una elección entre secretos, no una lista |
  | `TEAMS_*` | si `teams_enabled` | Encaja: es el mismo conjunto en los dos procesos |

  El descriptor gana un segundo eje **declarativo** (`feature`): la condición de
  activación, nombrada una vez, y una tabla en `secrets_for` que dice qué proceso la
  evalúa. Sin esto el refactor no puede reproducir el contrato actual, y el criterio 3
  (paridad exacta) es inalcanzable. **`required_in` pasa a ser «procesos donde el
  proceso puede preguntar»**, y quien lleva la decisión es `feature`.

- **Dos «secretos» no son secretos: son valores y guardas de tipo.** `TEAMS_NOTICES_KEY`
  se exige cuando está **vacía** (no cuando tiene placeholder: `reviews:notices` es un
  valor legítimo), y `SANDBOX_RUNNER_URL` directamente **no es un secreto**: es una
  URL que el `fix-worker` necesita para arrancar. Meterlos en el descriptor como
  ``Secret(placeholder="")`` produce falsos positivos (una `TEAMS_NOTICES_KEY` en
  `change-me` pasaría la comprobación). El descriptor admite dos campos más —
  `required_when_empty` y `expect: "value" | "url"`— y no es «un mini-framework»: son
  las dos formas que el inventario real ya tenía escondidas en `if`.

**Preguntas abiertas, resueltas:**

- **Q1 — placeholder.** Global: `PLACEHOLDER_VALUES = frozenset({"change-me", "changeme"})`
  en `settings.py`, y el runner lo importa (arriba). Los tres campos que hoy escriben
  `= "change-me"` duplican ese literal **a propósito**: su default documenta la forma
  y `.env.example` debe traer un valor (el test de `.env.example` prohíbe vacíos en
  secretos); van marcados con un comentario y sin duplicar la *lista*.
- **Q2 — secretos de petición.** Fuera del inventario de I1, como quería la
  alternativa conservadora: `_verify_signature` (API), `verify_session` (web),
  `_verify_token`/`_verify_console_token` (runner) y `teams_bot` son **guardas de
  petición**, deciden en cada petición y se quedan donde están. Lo que sí se unifica
  es el *predicado* (`is_missing` vía `PLACEHOLDER_VALUES`), que es lo que estaba
  cuadruplicado. Se descarta declararlos con un `request_side=True` extra: añadiría un
  quinto caso al descriptor justo cuando el criterio es que no crezca.
- **Q3 — `required_in` derivado del compose.** **No se deriva, y no basta con
  `required_in`** (ver el segundo punto): el contrato tiene dos ejes y el compose no
  conoce `VCS_PROVIDER`, `SANDBOX_ENABLED` ni el registro de modelos, que son
  condiciones de ejecución. El compose solo asigna variables por proceso, que es medio
  eje. Queda como comprobación de *[010](../../iniciativas/010-personas-de-wiring/spec.md)*
  (su comprobación (a)/(b) cruza `environment:` de compose con `Settings`), que es el
  sitio donde un par «servicio ↔ variable» se contrasta sin acoplar el modelo.

---

## 1. Objetivo

El contrato de secretos de producción (ADR-0013) se escribe hoy **a mano, secreto a
secreto**, en cinco funciones de `settings.py` (`insecure_secrets`,
`panel_insecure_secrets`, `fix_worker_insecure_secrets`, `teams_bot_insecure_secrets`)
y en cuatro copias del mismo predicado. Nada obliga a que un campo nuevo pase por ahí.

Se quiere que **el modelo de `Settings` sea la única declaración de qué es un
secreto, cuál es su valor de ejemplo y en qué procesos se exige**, y que el contrato
se derive de ahí:

1. Añadir un campo secreto y olvidar su comprobación deja de ser posible: el
   contrato lo cubre por construcción.
2. Un mismo secreto se juzga **igual** en todos los procesos (hoy
   `SANDBOX_RUNNER_TOKEN` no).
3. El valor de ejemplo (`change-me`) se declara **una vez**, no en cuatro sets
   literales que hoy pueden divergir.
4. Un test recorre **todos** los campos declarados secretos y comprueba que el
   contrato los exige, en vez de listarlos a mano.

## 2. No-objetivos

- **Cambiar la semántica de seguridad.** Es un refactor de *cómo* se declara el
  contrato, no de *qué* es seguro. Un secreto que hoy no se exige en producción
  sigue sin exigirse; solo cambia el mecanismo. Cualquier endurecimiento va aparte.
- **Dejar de aceptar vacío.** Vacío y `change-me` cuentan los dos como «sin
  configurar»; eso ya está decidido (`9fe30b0`) y no se revisa.
- **Un gestor de secretos** (Vault, SOPS, AWS Secrets Manager). Sigue entrando todo
  por entorno (ADR-0013).
- **Validar los secretos de la API y del runner desde `settings.py`.** La API valida
  la firma en cada petición (`_verify_signature`) y el runner su bearer
  (`_verify_console_token`): son guardas de *petición*, no de *arranque*, y se
  quedan donde están. Solo comparten la noción de placeholder (§3.4).
- **Rotación automática de secretos.** Es de [015](../015-puesta-en-marcha-real/spec.md).

## 3. Contexto y evidencia

### 3.1 Cuatro copias del mismo predicado

| Fichero | Constante | Qué hace |
|---|---|---|
| `settings.py:245` | `_INSECURE_DEFAULTS = {"change-me", "changeme"}` | Base del contrato de arranque |
| `api/main.py:31` | `_INSECURE_DECISION_TOKENS = {"change-me", "changeme"}` | Relaja el bearer de decisión |
| `web/auth.py:36` | `_INSECURE_SECRETS = {"change-me", "changeme"}` | Relaja la sesión del panel |
| `sandbox_runner/main.py:33` | `_INSECURE_TOKENS = {"change-me", "changeme"}` | Relaja el bearer del runner |

Cuatro definiciones idénticas, cuatro sitios que hay que tocar si el placeholder
cambia. Y `sandbox_runner/main.py:33` declara un set que **no usa**: sus dos
comprobaciones (líneas 48 y 63) son `not token or token in _INSECURE_TOKENS`, es
decir, ya hacen lo mismo que `is_missing` a mano.

### 3.2 El contrato se evalúa en cinco listas mantenidas a mano

`insecure_secrets` tiene 16 comprobaciones escritas una a una (VCS, Teams, LLM por
registro o por clave, sandbox, panel), y cada proceso tiene su propia variante:
`panel_insecure_secrets` (solo la sesión), `fix_worker_insecure_secrets` (URL y
token del runner), `teams_bot_insecure_secrets` (identidad y bearer del bot). El
`SANDBOX_RUNNER_TOKEN` aparece en **dos** de ellas con criterios distintos: eso es
exactamente lo que produjo el fallo de `9fe30b0`, y volverá a pasar mientras el
contrato no tenga una sola fuente.

### 3.3 Trece campos son secretos y solo tres lo declaran

`Settings` tiene 13 campos cuyo nombre acaba en `_token`, `_secret`, `_password` o
`_api_key`. Solo **tres** llevan `= "change-me"` en el propio campo
(`teams_bot_shared_secret`, `tooling_webhook_secret`, `reviews_decision_token`); el
resto llevan `= ""` y confían en el placeholder de `.env.example`. Dos convenciones
para lo mismo, y ninguna de las dos es una declaración: un campo nuevo puede no
seguir ninguna y nada lo delata. `.env.example` tiene 19 valores `change-me` y 4
variables vacías a propósito.

### 3.4 Las del código y las del arranque no son la misma cosa

Conviene no borrar las copias por parecer duplicadas: `api/main.py` decide en cada
petición si exige la firma; `web/auth.py` si relaja la sesión; el runner si exige su
bearer. Un refactor que solo unifique texto perdería esa distinción. Lo que sí se
puede compartir es la **lista de placeholders** (§5.2), y lo que hay que arreglar es
que `settings.py` no la repita.

## 4. Decisiones

| # | Decisión | Motivo |
|---|---|---|
| D1 | Un **descriptor** `Secret` (dataclass congelada) declara valor de ejemplo, obligatoriedad y procesos donde se exige | Es la declaración que hoy no existe; reemplaza los cinco bloques a mano |
| D2 | Los campos de `Settings` guardan el descriptor como **`metadata` del `Field`**, no en una tabla aparte | `model_fields` ya es el inventario del modelo: la declaración y el campo no pueden desincronizarse |
| D3 | **`required_in`** declara los procesos (`api`, `worker`, `fix-worker`, `web`, `teams-bot`) que exigen el secreto; **`feature`** declara la condición (ver la enmienda: `SANDBOX_RUNNER_TOKEN` deja de tener dos criterios según quién pregunte) | Una lista mantenida a mano es la causa raíz del fallo; el recorrido la elimina |
| D4 | El contrato se deriva con **`secrets_for(settings, process)`**, un único recorrido de `model_fields` | Una lista mantenida a mano es la causa raíz del fallo; el recorrido la elimina |
| D5 | El valor de ejemplo se declara en **un** sitio (`PLACEHOLDER_VALUES`) y los sets de API, web y runner delegan en él | Cuatro copias idénticas hoy; una sola si el placeholder cambia |
| D6 | **`require_*_secrets(settings)`** conserva su nombre y su firma, y pasa a delegar en `secrets_for` | Los puntos de arranque no cambian; el refactor no toca los procesos |
| D7 | `PLACEHOLDER_VALUES` vive en `settings.py`; el runner **ya importa** `settings` (`is_missing`, §Enmienda), así que la importa como API y web: no hay réplica que mantener | El aislamiento del runner es de red y de credenciales (ADR-0011), no de módulos |
| D8 | El test recorre **`Settings.model_fields`**, no una lista escrita a mano | Cierra I1 (§5.4): un campo secreto nuevo sin declarar se delata solo |

## 5. Diseño o contrato

### 5.1 El descriptor

```python
@dataclass(frozen=True)
class Secret:
    """Declaración de un secreto: dónde se pregunta, cuándo se exige y qué se espera."""

    # Procesos que pueden preguntar por este secreto. Vacío = todos.
    required_in: frozenset[str] = frozenset()
    # Condición de activación (nombre de `features`, §5.3). Vacío = siempre.
    feature: str | None = None
    # Se exige por estar VACÍO (no por placeholder): un valor no vacío es legítimo.
    required_when_empty: bool = False
    # Qué se espera del valor: un secreto (vacío o placeholder = ausente) o un valor
    # cualquiera (`url`, `value`): una URL no es un secreto.
    expect: str = "secret"
```

`expect` y `required_when_empty` existen porque el contrato real de hoy incluye
`SANDBOX_RUNNER_URL` (una URL que el `fix-worker` necesita para arrancar) y
`TEAMS_NOTICES_KEY` (se exige vacía, y `reviews:notices` es un valor legítimo).

### 5.2 Una sola lista de placeholders

```python
# `settings.py`
PLACEHOLDER_VALUES = frozenset({"change-me", "changeme"})
```

- `api/main.py`, `web/auth.py` y `sandbox_runner/main.py` importan de aquí y borran
  los cuatro sets literales (`_INSECURE_DECISION_TOKENS`, `_INSECURE_SECRETS`,
  `_INSECURE_TOKENS`) y las dos comparaciones a pelo (`secret == "change-me"`).
- El runner **ya importa** `settings` (§Enmienda), así que no hay réplica que
  mantener. Esto sustituye a D7/§5.2 originales.

### 5.3 El contrato derivado

```python
def secrets_for(
    settings: Settings,
    process: str,
    feature_enabled: Mapping[str, bool] | None = None,
) -> list[str]:
    """Nombres de los secretos sin configurar que este proceso exige al arrancar."""
```

Reglas:

1. Recorre `Settings.model_fields` y toma los que llevan un `Secret` en `metadata`.
2. Aplica el filtro de proceso: `not secret.required_in or process in secret.required_in`.
3. Evalúa la condición `feature` contra una tabla declarativa, no con `if` repartidos:

   | `feature` | Condición | Nota |
   |---|---|---|
   | `None` | siempre | — |
   | `sandbox` | `sandbox_enabled`, y **siempre** en el `fix-worker` | Su contrato entero es hablar con el runner: no tiene otro trabajo que justifique la condición |
   | `panel` | `panel_enabled`, y **siempre** en el `web` | El proceso `web` **es** el panel; la ausencia del flag no puede dejar un panel sin autenticación |
   | `teams` | `teams_enabled` | — |
   | `vcs_twg` / `vcs_github` | `vcs_provider == …` | Una elección: se exige uno de los dos, no los dos |
   | `llm_legacy` | `llm_reviewer_enabled and not models_config` | Vía de compatibilidad |
   | `llm_registry` | `llm_reviewer_enabled and models_config` | Los secretos los nombra el YAML |

   Las funciones reciben `(settings, process)`: solo `sandbox` y `panel` miran el
   proceso, y por eso está escrito aquí y no escondido en una lista. `feature_enabled`
   permite inyectar la tabla en los tests; por defecto se calcula de `settings`. Si la
   tabla no cubre una `feature` declarada, es un error al construir (no un secreto que
   deja de exigirse).
4. `expect` decide la comprobación:
   - `"secret"` → `is_missing(valor)` (vacío o placeholder).
   - `required_when_empty=True` → `not valor.strip()` (vacío, no placeholder).
   - `"value"` / `"url"` → `not valor.strip()`.
5. **El caso del registro de modelos** (`llm_registry`) no es un nombre de campo: los
   campos de `Settings` son `llm_api_key`, pero el nombre que se exige lo produce
   `models_config.unresolved_secrets` (p. ej. `MODELS_API_KEY de nan (LLM_API_KEY)`).
   Se resuelve en `secrets_for` delegando en `unresolved_secrets` cuando el registro
   está activo, con su test propio (R2).

Esta tabla es la única parte del contrato que no cabe en el descriptor; el resto son
datos. Cuatro de las cinco funciones actuales pasan a ser una llamada a `secrets_for`
con su proceso, y sus nombres y firmas se conservan (D6).

### 5.4 Invariantes

- **I1 — Ningún secreto sin declarar.** Para todo campo de `Settings` cuyo nombre
  termine en `_TOKEN`, `_SECRET` o `_PASSWORD`, o cuyo valor por defecto sea un
  placeholder, o que aparezca en `.env.example` con `change-me`, existe un `Secret`
  en su `metadata`. Un test lo comprueba sobre `model_fields`: un campo nuevo sin
  declarar pone el test en rojo. Los campos que hoy no son secretos pero el contrato
  exige (`sandbox_runner_url`) se declaran igual y quedan exentos de I1 por su
  `expect`, con la excepción nombrada.
- **I2 — Un secreto, un criterio (corregido).** El mismo secreto se juzga con la
  misma **condición** en todos los procesos. Hoy no es así: `SANDBOX_RUNNER_TOKEN`
  está en `sandbox` para `api`/`worker` y en `sandbox_always` para `fix-worker`, y eso
  es deliberado (el `fix-worker` no tiene otro trabajo que hablar con el runner).
  El invariante real, y el que se testea, es: **una sola declaración por campo, y
  el veredicto se deriva de ella**, de modo que no puede haber dos listas a mano que
  discrepen. La igualdad literal entre procesos no se exige; la unicidad de la
  fuente, sí.
- **I3 — Nombres cerrados.** `required_in` solo admite los procesos reales
  (`api`, `worker`, `fix-worker`, `web`, `teams-bot`); `feature` solo los nombres de
  la tabla de §5.3. Un nombre desconocido es un error al construir el descriptor.

### 5.5 Configuración

Sin cambios: es un refactor interno. `.env.example` y las variables no se tocan.

## 6. Riesgos

| # | Riesgo | Mitigación |
|---|---|---|
| R1 | **El refactor cambia el comportamiento sin querer** | No-objetivo §2: el test de paridad compara, secreto a secreto, el resultado nuevo con el actual **antes** de borrar lo viejo; cualquier diferencia es un fallo del refactor, no una mejora |
| R2 | **La excepción del registro de modelos se complica** | Es la única condición que no cabe en el descriptor; se aísla en una función propia con su test (vía por registro y vía por `LLM_API_KEY`) |
| R3 | **El contrato real no cabe en `required_in`** y el refactor «simplifica» cambiando el veredicto (`SANDBOX_RUNNER_TOKEN` en el fix-worker, `PANEL_SESSION_SECRET` en el web) | La enmienda y el segundo eje (`feature`) lo cubren; el test de paridad (R1) sobre los 48 escenarios del espacio es la red: una condición que diferencie da rojo |
| R4 | **El descriptor se convierte en un mini-framework** | Cinco campos, cada uno con un caso real medido (`expect` y `required_when_empty` los exige `SANDBOX_RUNNER_URL`/`TEAMS_NOTICES_KEY`). Si pide un sexto, la decisión vuelve a revisión en vez de crecer sola |
| R5 | **`metadata` es opaco para el lector** | `model_fields[name].metadata` no se ve al leer el campo; un test y un comentario en el módulo apuntan a `secrets_for` como la única vía de lectura |
| R6 | **El caso del registro de modelos** (`unresolved_secrets`, que devuelve un nombre que no es un campo) convive mal con el recorrido por `model_fields` | Se aísla en `secrets_for` con una rama propia y test doble (vía registro y vía `LLM_API_KEY`), como pedía R2 original |

## 7. Criterios de hecho

1. `PLACEHOLDER_VALUES` es la única definición de la *lista* de placeholders: `grep` de
   `{"change-me", "changeme"}` deja solo esa (más los `= "change-me"` de campo, que
   son valores por defecto, no listas). API, web y runner la importan; se borran
   `_INSECURE_DECISION_TOKENS`, `_INSECURE_SECRETS`, `_INSECURE_TOKENS` y las dos
   comparaciones a pelo.
2. Todo campo secreto de `Settings` declara su `Secret` (I1), y un campo nuevo sin
   declarar pone el test en rojo.
3. `secrets_for` reproduce **exactamente** el contrato actual: un test de paridad
   compara, sobre los 48 escenarios del espacio (proveedor de VCS × sandbox × panel ×
   teams × LLM), su resultado con el de las cuatro funciones de hoy antes de que estas
   deleguen en él.
4. El veredicto de `SANDBOX_RUNNER_TOKEN` (siempre en el `fix-worker`, con sandbox en
   `api`/`worker`) y el de `PANEL_SESSION_SECRET` (siempre en `web`) salen de la
   declaración, no de una lista: quitar la condición de la declaración pone el test de
   paridad en rojo.
5. Los puntos de arranque (`api`, `worker`, `fix-worker`, `web`, `teams-bot`) no
   cambian su comportamiento; sus tests de `production` siguen pasando sin tocarlos.
6. `make ci` y `make docs-check` en verde.

## 8. Preguntas abiertas

Todas resueltas el 2026-10-17 (ver la enmienda al principio del documento): Q1,
placeholder global; Q2, los secretos de petición se quedan fuera del inventario; Q3,
el contraste con el compose es de [010](../../iniciativas/010-personas-de-wiring/spec.md),
no del modelo.
