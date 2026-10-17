Última modificación: 2026-10-17 | Creado: 2026-10-17

# Contrato de secretos declarado en el modelo

> **Estado:** borrador
>
> **Motivo inmediato:** el refactor `is_missing` (commit `92dea1e`) se dejó cuatro
> comprobaciones con el predicado viejo: `TOOLING_WEBHOOK_SECRET` y
> `REVIEWS_DECISION_TOKEN` vacíos no fallaban en `production`, y `SANDBOX_RUNNER_TOKEN`
> se juzgaba distinto en el worker y en el fix-worker. Corregido en `9fe30b0`, pero el
> patrón que lo causó sigue ahí.
>
> **Decisión:** [ADR-0022](../../adr/0022-contrato-de-secretos-declarado.md).

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
| D3 | **`required_in`** declara los procesos (`api`, `worker`, `fix-worker`, `web`, `teams-bot`) que exigen el secreto | `SANDBOX_RUNNER_TOKEN` deja de tener dos criterios según quién pregunte |
| D4 | El contrato se deriva con **`secrets_for(settings, process)`**, un único recorrido de `model_fields` | Una lista mantenida a mano es la causa raíz del fallo; el recorrido la elimina |
| D5 | El valor de ejemplo se declara en **un** sitio (`PLACEHOLDER_VALUES`) y los sets de API, web y runner delegan en él | Cuatro copias idénticas hoy; una sola si el placeholder cambia |
| D6 | **`require_*_secrets(settings)`** conserva su nombre y su firma, y pasa a delegar en `secrets_for` | Los puntos de arranque no cambian; el refactor no toca los procesos |
| D7 | `PLACEHOLDER_VALUES` sigue viviendo en `settings.py`; quien no puede importarlo (el runner aislado) lo replica con un test que lo delata si diverge | El runner no debe depender del paquete para una constante de tres valores |
| D8 | El test recorre **`Settings.model_fields`**, no una lista escrita a mano | Cierra I1 (§5.4): un campo secreto nuevo sin declarar se delata solo |

## 5. Diseño o contrato

### 5.1 El descriptor

```python
@dataclass(frozen=True)
class Secret:
    """Declaración de un secreto: valor de ejemplo y procesos que lo exigen."""

    placeholder: str | None = None   # valor de ejemplo (None = admite vacío a propósito)
    required_in: frozenset[str] = frozenset()   # procesos que lo exigen en production
```

Cuatro formas de declararlo, y una sola de leerlo:

| Caso | Declaración | Quién lo exige |
|---|---|---|
| Secreto de todos los procesos | `Secret()` | cualquiera |
| Solo del worker | `Secret(required_in=frozenset({"worker"}))` | el worker |
| Del runner y del fix-worker | `Secret(required_in=frozenset({"fix-worker", "sandbox-runner"}))` | los dos |
| Variable sensible sin valor de ejemplo | `Secret(placeholder="")` | según `required_in` |

### 5.2 Una sola lista de placeholders

```python
# `settings.py`
PLACEHOLDER_VALUES = frozenset({"change-me", "changeme"})
```

- `api/main.py`, `web/auth.py` y `sandbox_runner/main.py` importan de aquí y borran
  sus cuatro sets.
- El runner, que no debe importar del paquete, replica la constante con un comentario
  que apunta a `settings.PLACEHOLDER_VALUES` y **un test comprueba que coinciden**:
  si divergen, el test falla en vez de dejar un agujero silencioso.

### 5.3 El contrato derivado

```python
def secrets_for(settings: Settings, process: str) -> list[str]:
    """Nombres de los secretos sin configurar que este proceso exige arrancar."""
```

Reglas:

1. Recorre `Settings.model_fields` y toma los que llevan un `Secret` en `metadata`.
2. Incluye el campo si `process in secret.required_in` **o** si `required_in` está
   vacío (secreto de todos).
3. Un secreto está «sin configurar» si su valor es `is_missing` (vacío o placeholder).
4. Excepciones declaradas, no dibujadas con `if/else` sueltos donde se pueda evitar:
   - `MODELS_CONFIG` con registro declarativo: los secretos son los que referencia el
     YAML (`models_config.unresolved_secrets`); `LLM_API_KEY` deja de exigirse por la
     vía de compatibilidad.
   - `TEAMS_*`: solo si `teams_enabled`.
   - `sandbox_*`: solo si `sandbox_enabled`.

Las tres condiciones de (4) son las únicas que no caben en el descriptor y quedan
como guardas explícitas en `secrets_for`, documentadas una a una.

### 5.4 Invariantes

- **I1 — Ningún secreto sin declarar.** Para todo campo de `Settings` cuyo nombre
  termine en `_TOKEN`, `_SECRET` o `_PASSWORD`, o cuyo valor por defecto sea un
  placeholder, existe un `Secret` en su `metadata`. Un test lo comprueba sobre
  `model_fields`: un campo nuevo sin declarar pone el test en rojo.
- **I2 — Un secreto, un criterio.** El mismo nombre de campo da el mismo veredicto
  en todos los procesos que lo exigen. Un test comprueba que
  `secrets_for(s, "worker")` y `secrets_for(s, "fix-worker")` coinciden en
  `SANDBOX_RUNNER_TOKEN`.
- **I3 — Nombres de proceso cerrados.** `required_in` solo admite los cinco procesos
  reales; un nombre desconocido es un error al construir (no un secreto que nadie
  exige nunca).

### 5.5 Configuración

Sin cambios: es un refactor interno. `.env.example` y las variables no se tocan.

## 6. Riesgos

| # | Riesgo | Mitigación |
|---|---|---|
| R1 | **El refactor cambia el comportamiento sin querer** | No-objetivo §2: el test de paridad compara, secreto a secreto, el resultado nuevo con el actual **antes** de borrar lo viejo; cualquier diferencia es un fallo del refactor, no una mejora |
| R2 | **La excepción del registro de modelos se complica** | Es la única condición que no cabe en el descriptor; se aísla en una función propia con su test (vía por registro y vía por `LLM_API_KEY`) |
| R3 | **El runner deja de ver el placeholder de `settings`** | §5.2: replica la constante con un test de coincidencia, en vez de importar el paquete desde la caja aislada |
| R4 | **El descriptor se convierte en un mini-framework** | Solo cuatro formas (§5.1). Si pide una quinta, la decisión vuelve a revisión en vez de crecer sola |
| R5 | **`metadata` es opaco para el lector** | `model_fields[name].metadata` no se ve al leer el campo; un test y un comentario en el módulo apuntan a `secrets_for` como la única vía de lectura |

## 7. Criterios de hecho

1. `PLACEHOLDER_VALUES` es la única definición del placeholder: `grep` de
   `{"change-me", "changeme"}` deja solo esa; API, web y runner delegan o replican
   con su test de coincidencia.
2. Todo campo secreto de `Settings` declara su `Secret` (I1), y un campo nuevo sin
   declarar pone el test en rojo.
3. `secrets_for` reproduce **exactamente** el contrato actual, con un test de paridad
   que compara contra las cinco funciones de hoy antes de retirarlas.
4. `SANDBOX_RUNNER_TOKEN` da el mismo veredicto en worker y fix-worker (I2).
5. Los puntos de arranque (`api`, `worker`, `fix-worker`, `web`, `teams-bot`) no
   cambian su comportamiento; sus tests de `production` siguen pasando sin tocarlos.
6. `make ci` y `make docs-check` en verde.

## 8. Preguntas abiertas

- **Q1 — ¿El placeholder se declara por campo o global?** `Secret(placeholder=…)`
  permite un caso por campo; un solo global es más simple. `TEAMS_BOT_SHARED_SECRET`
  y `TOOLING_WEBHOOK_SECRET` ya llevan `= "change-me"` como valor por defecto del
  campo, así que puede bastar con el global.
- **Q2 — ¿Se declaran también los secretos de la API y del runner** (marcados «de
  petición») para que I1 los vea, o se quedan fuera del descriptor? Declararlos los
  documenta en un sitio; no declararlos deja tres secretos fuera del inventario.
- **Q3 — ¿`required_in` se deriva del `docker-compose.yml`?** El compose ya dice qué
  variable recibe cada servicio. Derivarlo acopla el modelo al compose; declararlo a
  mano puede divergir de él. ¿Un test que compare ambos?
