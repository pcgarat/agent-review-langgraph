Última modificación: 2026-10-17 | Creado: 2026-10-16

# Todo: Panel de operación

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden y los
checkpoints, en [`plan.md`](plan.md). Spec aprobada el 2026-10-16. Acota D14 y D16 de
[`arquitectura.md`](../../../arquitectura.md) §7. Una tarea cerrada se marca `[x]` en el
**mismo commit** que el código. Los IDs son estables dentro del repo: `OP`.

---

## Bloque 1 — Recorrido (checkpoint C1)

- [x] **OP1** Módulo `src/agent_review/trace.py`: `read_trace(saver, thread_id)`
  - Reconstruye el recorrido desde el checkpointer: nodos ejecutados, orden real,
    `next` pendiente y `writes` de cada paso.
  - Aceptación: un hilo completado y uno pausado devuelven el recorrido correcto.
  - Enmienda 2026-10-16: no usa `graph.get_state_history` (exigiría el grafo
    compilado); reproduce su cálculo de `next` sobre la API del `saver`, que es lo
    que permite leerlo desde el `web` sin construir el grafo (D1 del plan).
- [x] **OP2** Checkpointer de **solo lectura** para un proceso sin worker
  - `build_reader_checkpointer`, con pool pequeño y **sin** `setup()`: el `web` lee
    los checkpoints que escribe el worker, no crea tablas ni compite por escritura.
- [x] **OP3** Modelo de salida del recorrido en el dominio (nodos, orden, punto de
  parada, pausa si la hay), añadido a `checkpointer._STATE_TYPES` si viaja por estado.
  - Enmienda 2026-10-16: es un **read model** derivado (`domain/trace.py`); no viaja
    por el estado del grafo, así que **no** entra en `_STATE_TYPES` y `test_trace` fija
    esa frontera.
- [x] **OP4** Test de `read_trace`: hilo completado, hilo pausado y hilo a medias.

## Bloque 2 — Progreso del worker (checkpoint C2)

- [x] **OP5** Puerto `ProgressPort` (`start_node`/`finish_node`) con `NullProgress` y
  `RedisProgress`; elección por configuración, como `build_chat`.
- [x] **OP6** Marca `reviews:active:<review_key>` (`node`, `started_at`, TTL holgado).
- [x] **OP7** *Seam* de envoltura de nodo (`node_wrapper`) compartible con la
  [018](../../018-trazas-de-la-review/spec.md) (D2 de la 018 / T3)
  - Debe **preservar** la firma del nodo, su `RetryPolicy` y el `Runtime` que recibe
    `persona`; una envoltura que los pierda rompe el fan-out o el reintento.
- [x] **OP8** Conectar la marca al worker (`build_runtime`) y limpiarla al cerrar el
  hilo (completado o pausado).
- [x] **OP9** Test: con `NullProgress` el grafo produce el mismo estado y los mismos
  nodos que sin envoltura.
- [x] **OP10** Test de la envoltura: `persona` conserva su `Runtime` y los nodos su
  `RetryPolicy` (riesgo R1 del plan).
  - Enmienda 2026-10-16 (verificado, no supuesto): el fan-out de `persona` corre en
    **paralelo**, así que la marca es un *hash* por review con un campo y un contador
    de ramas por nodo, no un único nodo. El sello (`started_at`) es el de la primera
    rama; la marca del nodo desaparece cuando cierra la última.

## Bloque 3 — Servicio web, lectura (checkpoint C3)

- [x] **OP11** Servicio `web` (FastAPI), raíz de composición nueva, con perfil de
  compose `panel` (W1 de la spec).
- [x] **OP12** Autenticación de sesión con rol (`PANEL_SESSION_SECRET`; `lector` y
  `operador`), sin reutilizar el bearer de decisión (W7).
- [x] **OP13** Endpoint de estado: cola (`reviews:jobs`), en vuelo (progreso), pausas
  por `kind`, descartes (`reviews:jobs:dead`) y presupuesto (`budget:<key>`).
- [x] **OP14** Endpoint de recorrido: expone `read_trace` (OP1) para una review.
- [x] **OP15** UI de lectura: vista global (cola, en vuelo, pausas, descartes) y vista
  de detalle con el recorrido.
- [x] **OP16** Test de compose: sin el perfil `panel` no se levanta ningún servicio
  nuevo.
- [x] **OP17** Test de auth: `401` sin sesión; el rol `lector` no ejecuta comandos.
- [x] **OP18** `Settings`, `.env.example` y [`operacion.md`](../../../operacion.md) §1–2
  (servicio y variables) y §5 (endpoints del panel).
  - Enmienda 2026-10-16: el secreto del panel entra en el contrato fail-closed de
    producción con contrato **propio** (`settings.panel_insecure_secrets` /
    `require_panel_secrets`): el proceso `web` no recibe credencial de VCS ni clave
    del LLM, así que no se le puede exigir el contrato del worker. En el listado
    compartido (`insecure_secrets`) solo aparece si `PANEL_ENABLED=true`, para no
    tumbar un despliegue sin panel.

## Bloque 4 — Consola del sandbox (checkpoint C4)

- [x] **OP19** `on_line` **opcional** (default `None`) en `ContainerRuntime.start`;
  los *fakes* y el runtime actual siguen valiendo.
- [x] **OP20** `DockerCliRuntime` con `Popen` y un hilo lector por flujo
  (`stdout`/`stderr`): **acumula** (el `VerifyReport` no cambia) **y** emite a
  `on_line`
  - Sin `stderr=STDOUT`: el informe mantiene ambos flujos separados (riesgo R5).
- [x] **OP21** Buffer circular en memoria del runner por `run_key`, con alta de
  suscriptores (efímero, como los workspaces de [ADR-0018](../../../adr/0018-workspace-de-lectura-en-el-runner.md)).
- [x] **OP22** Endpoint SSE `GET /verify/{run_key}/console` en el runner, con el
  bearer compartido.
- [x] **OP23** Test: con `on_line` el `VerifyReport` es idéntico; el timeout y la
  destrucción del contenedor no cambian.
- [x] **OP24** Proxy SSE en el `web` y vista de consola en vivo en la UI.- [x] **OP25** Test: `404` si el `run_key` no está en el buffer; un reinicio del
  runner pierde el buffer y no altera el informe.

## Bloque 5 — Comandos (checkpoint C5)

- [x] **OP26** `Cancelled` (hereda de `PermanentError`, D6 del plan) y marca
  `reviews:cancel:<review_key>`.
- [x] **OP27** La envoltura (OP7) comprueba la marca en la **frontera de nodo** y
  aborta; nunca a mitad de nodo (W4).
- [x] **OP28** El worker maneja `Cancelled`: **borra el hilo** del checkpointer,
  limpia el inbox y **no** reencola (D5 del plan)
  - Borrar el hilo es la guarda durable: un hilo abortado con `next` no vacío se
    reanudaría en una reentrega.
  - Nota 2026-10-16: el chequeo de la marca viaja como parámetro (`build_graph(..., cancel=...)`
    → `node_wrapper(..., cancel=...)`), igual que el puerto de progreso, en vez de un estado
    global de módulo: `build_graph` no conoce Redis, pero recibe un callable, no infraestructura.
    Sin chequeo, la frontera no consulta nada y el sistema se comporta igual que antes (W2).
- [x] **OP29** Endpoint `POST /reviews/{key}/cancel` (rol `operador`), que pone la
  marca y encola el comando.
- [x] **OP30** Semántica por estado: antes de publicar → cancelación limpia; después →
  solo «cancelar lo pendiente» (spec §5.4).
- [x] **OP31** Endpoint `POST /reviews/{key}/retry`: reencola un descarte de la DLQ
  con `attempts=0`.
- [x] **OP32** Endpoint `POST /reviews/{key}/resume`: reencola el `request` de una
  pausa (lo que hace una decisión, sin decisión).
- [x] **OP33** UI de comandos: botones según el estado, con el **actor** auditado.
- [x] **OP34** Tests por estado: cancelar antes/después de publicar, reintentar un
  descarte, reanudar, y que `Cancelled` **no** se reintenta.
- [x] **OP35** *(Trasladada el 2026-10-17 a [015](../../015-puesta-en-marcha-real/spec.md).)* *(Humana)* Decidir y asignar **quién** puede cancelar (Q4 de la spec);
  sin esto el bloque no se cierra.

## Bloque 6 — Cierre (checkpoint C6)

- [x] **OP36** ADR-0020: «panel como servicio aparte, *read model* + comandos,
  progreso por marca» (Q7 de la spec).
- [x] **OP37** `CONTEXT.md`: «panel», «progreso», «cancelación».
- [x] **OP38** [`arquitectura.md`](../../../arquitectura.md) §7 (D14 y D16 acotadas) y
  §2/§5 si el servicio o las claves lo requieren.
- [x] **OP39** [`operacion.md`](../../../operacion.md) §7 (diagnóstico del panel y de la
  consola del sandbox).
- [x] **OP40** [`desarrollo.md`](../../../desarrollo.md) §2 (zonas de test nuevas) y §3
  (extensión: puerto de progreso y envoltura de nodo).
- [x] **OP41** `make ci` y `make docs-check` en verde.
- [x] **OP42** Anotar en la [018](../../018-trazas-de-la-review/spec.md) la coordinación
  del *seam* de nodos (D2 del plan).

## Bloque 7 — Correcciones de la revisión de código (checkpoint C7)

Hallazgos confirmados de la revisión de la 019 antes de subirla. Checkpoint: los
tests de regresión fallan con el código anterior y `make ci` queda en verde.

- [x] **OP43** CSRF en los comandos por cookie: `require_command_session` exige
  `Content-Type: application/json` y `Origin`/`Sec-Fetch-Site` del mismo origen
  cuando la sesión llega por cookie (`web/auth.py`).
- [x] **OP44** Cancelar una pausa de fix no pone la marca: el job resuelve la wave como
  no verificable y la reanudación tardía del fix-worker es un no-op.
- [x] **OP45** La marca de cancelación queda como *tombstone* tras abortar, para que
  un job rezagado no re-ejecute la review desde `START`.
- [x] **OP46** La marca lleva TTL (`CANCEL_TTL_S`) y «Reintentar» la retira antes de
  reencolar.
- [x] **OP47** El recorrido marca `in_progress` un hilo con un nodo despachado y sin
  cerrar (nodo vivo o worker muerto a mitad), no `completed`.
- [x] **OP48** El proxy de la consola resuelve la respuesta del runner antes de abrir
  el stream y da `404` en vez de un `200` vacío.
- [x] **OP49** El servicio `web` de compose recibe `SANDBOX_RUNNER_URL` y un bearer de
  solo lectura de la consola (`SANDBOX_CONSOLE_TOKEN`), que el runner acepta solo en
  `GET /verify/{run_key}/console`.
- [x] **OP50** «Reintentar» saca el descarte de la DLQ de forma atómica
  (`take_dead_letter`) y lo devuelve si reencolar falla: un segundo clic da `409`.
- [x] **OP51** El actor auditado de un comando es el de la sesión; el cuerpo de la
  petición ya no puede suplantarlo.
