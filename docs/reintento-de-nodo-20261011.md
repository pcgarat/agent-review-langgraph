Última modificación: 2026-10-11 | Creado: 2026-10-11

# Reintento de nodo: `RetryPolicy` con clasificación propia de errores

Hasta ahora el repo reintentaba **una sola cosa**: el job entero, reencolándolo en
Redis (`_consume` → `_requeue_or_drop`, con `attempts` y DLQ). Eso está bien para un
fallo que deja la review a medias, pero es la herramienta equivocada para un fallo
**transitorio y local**: un `twg` que devuelve un 500 una vez, un LLM con rate limit,
un webhook de Teams caído un segundo. Pagar por eso el precio de descartar el grafo y
reconstruirlo desde `START` es caro y —cuando la review ya pausó y tiene una decisión
humana en el estado— peligroso (ver «Relación con la revisión de `main`»).

Este cambio añade la capa que faltaba: **`RetryPolicy` por nodo**, para que el fallo
transitorio se reintente donde ocurrió, sin sacar el job de la cola.

---

## 1. Las dos granularidades (y por qué hacen falta las dos)

| Granularidad | Quién la ejecuta | Reintenta | Unidad de estado |
|---|---|---|---|
| **Nodo** | LangGraph (`retry_policy` en `add_node`) | La llamada que falló, con backoff | El checkpoint del hilo |
| **Job / review** | El worker (`_consume`) | La review entera, reencolada | El mensaje de la cola (`attempts`, DLQ) |

- El **nodo** es para fallos cortos y locales: no cambia el estado del hilo, no
  reencola, no toca el contador de intentos del job.
- El **job** sigue siendo la red de seguridad para lo que el nodo no puede resolver
  (fallo permanente, estado a medias, excepción fuera del grafo). No se retira.

Un fallo transitorio agotado en el nodo **propaga** y entonces sí lo recoge la capa de
job: las dos son complementarias, no alternativas.

## 2. La trampa: el `default_retry_on` de LangGraph no sirve aquí

La skill (y el módulo 08 de `docs/edu/`) dicen «usa `RetryPolicy(max_attempts=3)`».
Con el `retry_on` por defecto eso **no reintentaría ningún fallo de `twg` ni un
`429`**. Sí reintentaría los fallos HTTP «crudos» (5xx, conexión, timeout de
transporte), como un 500 de Teams, que el adaptador deja salir sin envolver.
Verificado contra `langgraph.types.default_retry_on` en 1.2.14:

```
ConnectError            -> True
ReadTimeout             -> True
HTTPStatusError 503     -> True
HTTPStatusError 429     -> False   # rate limit -> no reintenta!
RuntimeError            -> False   # de aqui heredan TwgError y LlmError
TimeoutError            -> False
OSError                 -> False
ConnectionError         -> True
```

Dos consecuencias directas:

1. `TwgError` y `LlmError` heredaban de `RuntimeError`, así que el default **jamás**
   habría reintentado un fallo de `twg` o del modelo. Hacerlas hijas de
   `TransientError` no lo arregla por sí solo: `TransientError` sigue siendo un
   `RuntimeError` y el default lo sigue descartando. Hace falta un `retry_on` propio.
2. Un `429` (rate limit) tampoco se reintenta, que es justo el caso que sí se resuelve
   esperando.

Por eso el grafo usa su propia política: `agent_review.errors.is_transient`.

## 3. Clasificación: el tipo del error es la política

`src/agent_review/errors.py` define la taxonomía y `is_transient`, el predicado único que
se pasa como `retry_on`. **No** es un `try/except` en cada adaptador: es una decisión
sobre el *tipo*.

| Se reintenta (`True`) | No se reintenta (`False`) |
|---|---|
| `TransientError` y sus hijas (`TwgError`, `LlmTransientError`) | `PermanentError` (y `LlmError`: 4xx, respuesta malformada) |
| `httpx.RequestError` (conexión, DNS, timeout) | Otros `httpx.HTTPStatusError` (4xx) |
| `httpx.HTTPStatusError` con 5xx o **429** | `ValueError`, `TypeError`, `KeyError`… |
| `subprocess.TimeoutExpired`, `TimeoutError`, `ConnectionError` | todo lo demás |

Deliberadamente **distinto** del default: se reintenta lo que *puede* cambiar entre
intentos y se trata todo lo demás como bug. En el caso ambiguo (el CLI de `twg` no
distingue «PR inexistente» de «500») se elige reintentar y acotar: la política da un
máximo de intentos y luego propaga.

## 4. Qué nodos llevan la política (y cuáles no)

```python
graph.add_node("scope", scope, retry_policy=retry)      # twg (diff, diffstat)
graph.add_node("persona", persona, retry_policy=retry)  # twg + LLM (y cubre cada Send)
graph.add_node("merge", merge)                          # puro
graph.add_node("policy", apply_policy)                  # puro
graph.add_node("hitl", hitl)                            # interrupt: nunca reintentar
graph.add_node("publish", publish, retry_policy=retry)  # twg
graph.add_node("notify", notify, retry_policy=retry)    # twg (URL del PR) + Teams
```

- **`hitl` no la lleva a propósito.** Un reintento re-ejecutaría `interrupt` y
  duplicaría la pausa. Además, el nodo tiene que seguir siendo el punto puro de
  sincronización con la persona.
- **`merge` y `policy` no la llevan** porque son deterministas: un fallo ahí es un bug.
- **`persona` degrada en el último intento.** `LlmReviewer` deja subir los fallos
  transitorios del modelo (antes los capturaba y devolvía 0 hallazgos, así que la
  política no tenía nada que reintentar). El nodo los relanza mientras quedan
  intentos (`runtime.execution_info.node_attempt < max_attempts`) y, en el último,
  devuelve 0 hallazgos de esa persona con un aviso en el log. Un proveedor caído no
  manda la review a la DLQ ni se lleva por delante los hallazgos de la estática. Los
  fallos permanentes del modelo (4xx, respuesta malformada) siguen degradando en el
  propio `LlmReviewer`, sin reintentar.
- **`persona` es un nodo con fan-out (`Send`)**: la política registrada en
  `add_node("persona", ...)` se aplica a las tareas creadas por el `Send`. Verificado
  en 1.2.14: dos ramas que fallan una vez cada una producen 4 llamadas y completan.

## 5. Idempotencia bajo reintento (por qué es seguro)

Un reintento de nodo **re-ejecuta el nodo desde el principio**. Eso obliga a que los
nodos con efectos sean idempotentes. Lo que hace que lo sean aquí:

1. **LangGraph descarta las escrituras del intento fallido** (`task.writes.clear()` al
   empezar cada intento). Verificado: un nodo que escribe `findings` y después lanza
   tres veces deja **una** entrada, no tres. Sin esto, el reducer `operator.add`
   acumularía basura. Aun así, cada nodo limpia sus campos que se acumulan antes de
   escribir (`scope` devuelve `findings: []`) para no depender de ello.
2. **`publish` relee las marcas del PR al empezar** (`_published_markers`), así que un
   reintento tras un fallo del resumen no repite los inlines ya escritos ni el
   resumen ya publicado. Es exactamente la misma idempotencia del dedupe de Fase 1.
   Teams ya no está en `publish`: va en el nodo `notify`, así que reintentar el aviso
   no vuelve a pasar por la publicación.
3. **`scope` y `persona`** no tienen efectos externos (leen); un reintento solo paga
   otra vez la llamada.

Un nodo sin efectos no necesita nada; un nodo con efectos ya era idempotente por el
dedupe de publicación. No se ha añadido `retry_policy` a ningún nodo nuevo con efectos.

## 6. El techo que la política **no** puede dar: `twg` sin `timeout`

`RetryPolicy` reintenta lo que **lanza**. Un `twg` que se cuelga no lanza nunca, así
que ningún reintento se dispara: el nodo se queda bloqueado y el worker con él.

LangGraph tiene `add_node(..., timeout=...)`, pero **no aplica a nodos síncronos** —
verificado: `ValueError: Node timeouts are only supported for async nodes because sync
Python execution cannot be safely cancelled in-process`. Los nodos de este grafo son
síncronos, así que el corte tiene que estar donde se bloquea: en el `subprocess`.

Por eso `TwgVcsAdapter._run` pasa `timeout=TWG_TIMEOUT_S` y convierte el
`subprocess.TimeoutExpired` en `TwgError` (recuperable): un `twg` colgado pasa a ser un
fallo transitorio que la política sí reintenta. `check_twg`, en el arranque, también
lleva `timeout` pero no se reintenta a propósito: si falla, se quiere fallar ruidoso.

## 7. Configuración

| Setting | Default | Qué hace |
|---|---|---|
| `NODE_RETRY_MAX_ATTEMPTS` | `3` | Intentos totales por nodo. `1` desactiva la política. |
| `NODE_RETRY_INITIAL_INTERVAL_S` | `0.5` | Espera antes del primer reintento. |
| `NODE_RETRY_BACKOFF_FACTOR` | `2.0` | Multiplicador entre reintentos. |
| `NODE_RETRY_MAX_INTERVAL_S` | `8.0` | Techo de la espera. Se acota para no clavar el worker secuencial. |
| `TWG_TIMEOUT_S` | `60.0` | Techo de una llamada a `twg`. |

El *default* de la política vive en el grafo (`graph.build.default_retry_policy`) para
que los tests no dependan del entorno; el worker la parametriza con `node_retry_policy`.

## 8. Lo que se ha descartado

| Opción | Por qué no |
|---|---|
| `default_retry_on` | No reintenta `RuntimeError` (nuestros adaptadores) ni `429` ni `TimeoutError` (§2). |
| `add_node(..., timeout=...)` | Solo para nodos async; los nuestros son síncronos (§6). |
| `add_node(..., error_handler=...)` | Añade un nodo de compensación por nodo; sin un caso real de compensación (Fase 3: revertir el fix) es capacidad adelantada. |
| Reintentar en cada adaptador a mano | Reparte la misma política en N sitios y hace que el grafo no pueda decidirla. |
| Reintentar también en `hitl` | Duplicaría la pausa. |

## 9. Relación con la revisión de `main`

`docs/revision-main-20261011.md` describe tres fallos relacionados. El reintento de
nodo los mitiga y los arreglos de `tasks/todo-revision-main.md` los cierran:

- **B2 — un reintento de job re-ejecuta el grafo desde `START`** (persona llamada dos
  veces, `findings` acumulados). Con el reintento de nodo, un fallo transitorio de
  `publish` se recupera **dentro** de la invocación. Cuando se agota y el job se
  reencola, `process_request` ve el hilo a medias y lo continúa con
  `invoke(None, config)` desde el nodo que falló: tampoco hay una pasada nueva desde
  `START`. Juntos, el checkpoint pasa a ser la unidad de reintento.
- **B8 — `twg` sin `timeout`**: es el §6 de este documento. `check_twg` también lleva
  `timeout` (`TWG_TIMEOUT_S`): no se reintenta, pero un `twg` colgado en el arranque
  falla ruidoso en vez de bloquear.
- **B9 — un fallo de Teams cuenta como fallo de la review**: el aviso sale de
  `publish` a un nodo `notify` propio, con la misma política. Un fallo transitorio se
  reintenta en el nodo y, si se agota, el reintento del job continúa en `notify` (B2)
  sin volver a publicar. Un fallo no transitorio se registra y no tumba una review ya
  publicada.

## 10. Verificación

`tests/test_retry.py` (12 tests) fija el comportamiento:

- La tabla de clasificación (`is_transient`) completa, incluido el `429` y el 404.
- El contraste con `default_retry_on` (el porqué de la política propia).
- `timeout` pasado al `subprocess` y el colgado convertido en `TwgError`.
- `scope` reintenta un fallo transitorio y completa la review.
- Un `ValueError` **no** se reintenta (una sola llamada).
- La persona LLM reintenta un fallo transitorio del modelo y, si se agotan los
  intentos, degrada a 0 hallazgos sin tumbar la review; un fallo permanente
  degrada sin reintentar.
- Una `persona` que falla y se recupera **no duplica** hallazgos (reducer + `writes.clear()`).
- `publish` reintentado no repite el inline ya publicado.
- La asignación de política por nodo: la llevan todos los nodos con efectos externos
  (`scope`, `persona`, `publish`, `notify`) y **no** la llevan `merge`, `policy`
  (deterministas) ni `hitl` (duplicaría la pausa).

Las regresiones de B2, B8 y B9 están en `tests/test_revision_main.py`.
