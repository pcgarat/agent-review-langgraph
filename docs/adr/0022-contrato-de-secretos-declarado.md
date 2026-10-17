# ADR-0022 — El contrato de secretos se declara en el modelo

- **Estado:** Aceptado
- **Fecha:** 2026-10-17

## Contexto

[ADR-0013](0013-secretos-por-entorno-fail-closed.md) fija **qué** es un secreto y
qué pasa si falta: en `production` el proceso no arranca. Ese contrato se escribe a
mano, secreto a secreto, en cinco funciones de
[`settings.py`](../../src/agent_review/settings.py) y con cuatro copias idénticas del
set de placeholders (`settings.py`, `api/main.py`, `web/auth.py`,
`sandbox_runner/main.py`).

El refactor del placeholder (`change-me` cuenta como sin rellenar, commit `92dea1e`)
se dejó cuatro comprobaciones con el predicado viejo. Consecuencia medida en
`production`, con el resto de secretos reales:

- `TOOLING_WEBHOOK_SECRET=""` y `REVIEWS_DECISION_TOKEN=""` arrancaban **sin fallar**;
- `SANDBOX_RUNNER_TOKEN=""` **pasaba** en el worker y **fallaba** en el fix-worker:
  el mismo secreto, dos criterios, porque aparece en dos listas mantenidas a mano.

Corregido en `9fe30b0`. La causa raíz no: nada obliga a que un campo nuevo pase por
el contrato, ni a que el mismo secreto se juzgue igual en todos los procesos.

## Decisión

- **El descriptor es la declaración.** Un `Secret` (dataclass congelada) con el valor
  de ejemplo y los procesos que lo exigen vive en el `metadata` del `Field` del campo
  de `Settings`. El inventario del modelo (`model_fields`) y la declaración de
  seguridad son el mismo sitio, así que no pueden desincronizarse.
- **`required_in` declara los procesos** (`api`, `worker`, `fix-worker`, `web`,
  `teams-bot`) **donde el contrato aplica**, y **`feature` declara la condición** de
  activación. Son dos ejes porque el mismo secreto se exige con condiciones distintas
  según el proceso: `SANDBOX_RUNNER_TOKEN` es «si `SANDBOX_ENABLED`» en el worker y
  «siempre» en el fix-worker (cuyo único trabajo es hablar con el runner), y
  `PANEL_SESSION_SECRET` es «si el panel está desplegado» en API/worker y «siempre» en
  el `web`, que **es** el panel. Declarar solo los procesos perdía esa distinción:
  `feature` la nombra una vez y `secrets_for` la evalúa.
- **El contrato se deriva con `secrets_for(settings, process)`**, un único recorrido
  de `model_fields`. Las cinco funciones actuales se conservan con su nombre y firma
  y pasan a delegar: los puntos de arranque no cambian.
- **`PLACEHOLDER_VALUES` es la única definición** del valor de ejemplo. La API, la web
  y el runner la importan. El runner **ya importa** `settings` (`is_missing`, para
  normalizar su credencial de registro), así que no se replica nada: el aislamiento
  del runner ([ADR-0011](0011-sandbox-runner-aislado.md)) es de red y de credenciales,
  no de módulos.
- **Los secretos «de petición» no entran en el inventario de arranque.** La firma del
  webhook, el bearer del runner, la sesión del panel y el bearer de la consola deciden
  en cada petición, no al arrancar; se quedan donde están y solo comparten el
  predicado (`is_missing`). Los campos que sí se declaran para que el inventario
  (`I1`) sea completo pero que el contrato de hoy no exige (`TWG_TOKEN` de Fase 4,
  `SANDBOX_REGISTRY_TOKEN`, `SANDBOX_CONSOLE_TOKEN`) usan `feature="solo_inventario"`.
- **Vacío y placeholder cuentan igual** («sin configurar»): vacío es el caso más
  probable (variable sin asignar, `VAR=`), y un contrato fail-closed que no lo cubre
  falla justo cuando más importa.
- **Las guardas de petición se quedan donde están.** La firma del webhook
  (`api/main.py`) y el bearer del runner (`sandbox_runner/main.py`) deciden en cada
  petición, no en el arranque; solo comparten la noción de placeholder.

## Consecuencias

- Añadir un campo secreto y olvidar su comprobación deja de ser posible: el
  contrato lo cubre por construcción, y un test (I1) delata el campo sin declarar.
- Un secreto tiene un solo veredicto en todos los procesos que lo exigen (I2). El
  fallo de `92dea1e` no puede repetirse por esa vía.
- El valor de ejemplo se cambia en un sitio, no en cuatro.
- `Settings` gana un atributo de modelo que **no se ve al leer el campo**: la
  declaración es accesible solo por `model_fields[...].metadata`. Es el precio de no
  tener una tabla paralela que pueda quedar desactualizada; se compensa con
  `secrets_for` como única vía de lectura y un comentario en el módulo.
- El descriptor admite cinco campos (`required_in`, `feature`, `required_when_empty`,
  `expect`) y no debe crecer más: `expect` y `required_when_empty` los exige el
  contrato real (`SANDBOX_RUNNER_URL` no es un secreto; `TEAMS_NOTICES_KEY` se exige
  por estar vacía y `reviews:notices` es un valor legítimo). Si aparece un sexto caso,
  la decisión vuelve a revisión en vez de que el descriptor se vuelva un mini-framework.
- **`Settings` pasa a ser un punto de serialización del trabajo en paralelo.** El
  inventario y la declaración comparten sitio *a propósito* (es el valor del ADR),
  pero el precio es que la declaración de un campo vive **en el campo**: no hay zona
  neutra. Dos ramas que añadan campos a `Settings` tocan las mismas líneas, y —más
  sutil— una rama que salga de un commit **anterior a este ADR** no conoce `I1`: si
  añade un campo con nombre de secreto, su rama está verde y la declaración no le
  falta, así que el rojo aparece al integrar, en `main`, por un test del que su rama
  no tiene contexto. La mitigación es que el mensaje de `I1` nombre este ADR (abajo);
  el coste no se puede eliminar sin renunciar a que la declaración viva en el campo,
  que es lo que evita que se desincronice.

---

> **Enmienda (2026-10-17).** Este ADR se aceptó junto con la implementación, con dos
> correcciones sobre el texto de arriba: (1) el runner importa `PLACEHOLDER_VALUES` en
> vez de replicarlo, porque ya importa `settings`; (2) el contrato necesita dos ejes
> (`required_in` + `feature`) para reproducir los veredictos reales, y el descriptor
> gana `expect` y `required_when_empty`. El detalle está en la
> [enmienda de la spec](../iniciativas/archivo/020-secretos-declarados/spec.md).
- Las tres excepciones que no caben en el descriptor (el registro declarativo de
  modelos, `teams_enabled`, `sandbox_enabled`) quedan como guardas explícitas y
  documentadas en `secrets_for`, no repartidas por el módulo.
- ADR-0013 sigue vigente en el **qué** (entorno, fail-closed, rotación); este ADR
  cambia el **cómo** se declara.

## Alternativas descartadas

- **Dejarlo como está y tener más cuidado.** Es lo que se intentó en `92dea1e`: la
  corrección se dejó cuatro sitios. Un refactor manual no arregla un problema de
  repetibilidad.
- **Validadores de Pydantic (`model_validator`) que fallen al construir.** El fallo
  sería al instanciar `Settings`, y `Settings` se construye también en tests, en el
  CLI y en `development`, donde los placeholders son legítimos: no distingue
  proceso, y el error aparecería lejos del arranque que lo provoca.
- **Un fichero de declaración aparte** (YAML o tabla en módulo). Separa la
  declaración del campo que declara: puede quedar desactualizada, que es
  precisamente el fallo que se está corrigiendo. Mismo motivo por el que no se
  deriva `required_in` del `docker-compose.yml`.
- **Compartir los secretos de petición con el arranque.** Meter la firma del webhook
  en el contrato del worker lo obligaría a tener un secreto que no usa; y en
  `production` el worker no debe fallar por algo que solo necesita la API.
- **Importar `PLACEHOLDER_VALUES` desde el runner.** *Descartada en la enmienda del
  2026-10-17:* se descartó por aislamiento, pero el runner **ya** importa `settings`
  (`is_missing`), así que el aislamiento no aplica a los módulos. Importar evita un
  duplicado que puede divergir, que es justo el fallo que este ADR corrige.
