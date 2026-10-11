Última modificación: 2026-10-11 | Creado: 2026-10-11

# Revisión de `main`: fallos, robustez y patrones

Revisión completa de la rama `main` (commit `1532414`) a 2026-10-11. El CI está en
verde (`ruff`, `mypy` strict, 227 tests) y la base de puertos y adaptadores está bien
planteada. Los problemas se concentran en las rutas de **reintento, reanudación y
fallo de infraestructura**, que es justo donde los tests no llegan.

Leyenda: 🔬 = reproducido con un script contra el código actual (fakes de
`tests/test_worker.py`, sin tocar el repo). El resto sale de la lectura del código.

> **Estado (2026-10-11):** los fallos B1–B10 de la §1 están arreglados, cada uno con
> su test de regresión (`tests/test_revision_main.py`, `tests/test_api.py`). El
> detalle, las decisiones tomadas (B4, B5) y los riesgos residuales están en
> `tasks/todo-revision-main.md`. Las §2–§5 siguen abiertas como propuesta.

**Prioridad recomendada:** B1 → B2 + B9 (patrón P2) → B3 → B7 + fallbacks a memoria
(R1). Cada arreglo con su test de regresión; los escenarios de reproducción de este
documento se pueden convertir en tests casi tal cual.

---

## 1. Fallos

| Id | Gravedad | Resumen | Dónde |
|---|---|---|---|
| B1 🔬 | Alta | Mandar a la DLQ un job con decisión tumba el worker | `src/agent_review/queue.py` (`_dead_letter_payload`) |
| B2 🔬 | Alta | Un reintento re-ejecuta el grafo desde `START` | `src/agent_review/worker/main.py` (`process_request`) |
| B3 🔬 | Alta | Una pausa vencida cuyo resume falla esquiva la DLQ para siempre | `src/agent_review/paused_store.py` (`sweep`), `worker/main.py` |
| B4 🔬 | Media | Un artefacto tardío en la misma ejecución nunca publica la review completa | `worker/main.py` (rama «ya completada») |
| B5 🔬 | Media | El merge por `(file, title)` se come hallazgos | `src/agent_review/graph/build.py` (`_merge_key`) |
| B6 | Media | El webhook acepta cualquier `repo` y revisa el PR del repo configurado | `src/agent_review/api/main.py`, `adapters/twg_vcs.py` |
| B7 | Media | La idempotencia se reclama antes de hacer el trabajo | `api/main.py` (`tooling_webhook`) |
| B8 | Media | `twg` se lanza sin `timeout` | `adapters/twg_vcs.py` (`_run`, `check_twg`) |
| B9 | Baja | Un fallo de Teams cuenta como fallo de la review y la notificación se pierde | `graph/build.py` (`publish`) |
| B10 | Baja | El aviso de pausa nunca lleva enlace al PR | `worker/main.py` (`_pr_url`) |

### B1 🔬 — Mandar a la DLQ un job con decisión tumba el worker

`_dead_letter_payload` serializa con `request.model_dump()`, que deja
`HitlDecision.decided_at` como `datetime`. `json.dumps` lanza `TypeError` **dentro
del `except` de `_consume`**: la excepción sale de `run_forever` y mata el proceso.
El job ya se había sacado de la cola con `BLPOP`, así que se pierde.

- **Cuándo pasa:** cualquier reanudación HITL (decisión humana o `expired` del TTL)
  que agote `MAX_REVIEW_ATTEMPTS`.
- **Reproducción:** pausa vencida + grafo que siempre falla → `sweep_expired` +
  `_consume` → `TypeError: Object of type datetime is not JSON serializable`.
- **Arreglo:** `request.model_dump(mode="json")`. Además, `_consume` no debería
  dejar escapar una excepción del propio manejo de errores.

### B2 🔬 — Un reintento re-ejecuta el grafo desde `START`

Si `publish` falla a medias, el reintento entra por la rama «a medias» de
`process_request` e invoca `graph.invoke({"request": ..., "findings": []}, config)`.
Con un input nuevo LangGraph arranca otra pasada desde `START`, no continúa desde el
nodo que falló. Medido:

- Las personas se ejecutan **2 veces**: el LLM se paga dos veces.
- `findings` se **acumula** (2 en vez de 1): el reducer `operator.add` no se puede
  vaciar devolviendo `[]` desde `scope`.
- **Lo grave, en HITL:** si la reanudación falla, el reintento recalcula los
  hallazgos (el LLM no es determinista) pero `human_decisions` sigue en el estado.
  El nodo `hitl` no vuelve a pausar, así que un hallazgo **nuevo** que pediría
  escalado se publica sin que nadie lo haya decidido.

**Reproducción:** grafo con `max_autonomy: fix_with_approval` → pausa → resume con
`post_comment` que falla una vez → reintento con `attempts=1`. Resultado: persona
llamada 2 veces, 2 hallazgos acumulados en el estado.

**Arreglo:** para un hilo con `next` no vacío y sin `interrupts`,
`graph.invoke(None, config)` (reanuda desde el último checkpoint). Ver también P2.

### B3 🔬 — Una pausa vencida cuyo resume falla esquiva la DLQ para siempre

`PausedStore.sweep()` no borra la entrada (a propósito, para que el resume sea
idempotente). Pero si el resume falla siempre:

1. El job agota sus 3 intentos y va a la DLQ.
2. 30 s después, `sweep_expired` vuelve a encolarlo con `attempts=0` (usa
   `review.request` original).
3. Otros 3 intentos, otra entrada en la DLQ, y así hasta que caduque la clave de
   Redis (8 días).

**Reproducción:** 3 barridos → DLQ = 3 entradas y la pausa sigue en el inbox.

Relacionado: la rama «ya completada» de `process_request` tampoco limpia el inbox.
Si el worker muere entre `graph.invoke` y `paused.resume`, la entrada se queda y el
barrido la reencola en bucle (cada pasada es un no-op, pero nunca se limpia).

**Arreglo:** marcar la entrada como «reanudación en curso» (o guardar el número de
intentos en el inbox) y limpiar el inbox también en la rama «ya completada».

### B4 🔬 — Un artefacto tardío en la misma ejecución nunca publica la review completa

El README afirma que una review degradada (`:partial`) no bloquea a una completa
posterior «ni de la misma ejecución si el artefacto llega tarde». No es así:

1. El TTL degrada la ingesta y el worker publica la review parcial.
2. Llega el artefacto: `observe` pasa a `READY`, la API reclama `review_key` (el
   barrido no lo había reclamado) y encola.
3. El worker ve el hilo con el **mismo `thread_id`** ya completado → no-op.

**Reproducción:** primera pasada con `MISSING` → `complete=False`, 1 comentario.
Segunda con `PRESENT`, misma `ReviewRequest` → sigue `complete=False`, 1 comentario.

**Arreglo:** decidir el comportamiento deseado y alinear código y README. Si la
completa debe publicarse, el `thread_id` tiene que distinguir la pasada degradada
(p. ej. sufijo `:partial`), o el worker debe re-ejecutar cuando el hilo terminó
incompleto y ahora la fuente está presente.

### B5 🔬 — El merge por `(file, title)` se come hallazgos

`_merge_key` deduplica por `(fichero, título)`. Dos errores de PHPStan con el mismo
mensaje en líneas distintas del mismo fichero (muy habitual: «Call to an undefined
method…», «Parameter #1 … expects …») se quedan en uno.

**Reproducción:** dos hallazgos iguales en las líneas 1 y 40 de `a.php` → `merged`
= 1, inlines = 1.

**Arreglo:** incluir la línea en la clave, o una ventana de líneas si se quiere
fusionar el mismo problema detectado por estática y por LLM con anclas cercanas.

### B6 — El webhook acepta cualquier `repo`

`TwgVcsAdapter` ignora el parámetro `repo` (`del repo`) y siempre usa
`BITBUCKET_WORKSPACE`/`BITBUCKET_REPO`. `POST /webhook/tooling` no valida el `repo`
del cuerpo: un CI de otro repo (o un error de configuración) hace que el agente
revise **y comente** el PR #N del repo configurado.

**Arreglo:** rechazar en la API (400/422) cualquier `repo` distinto de
`{workspace}/{repo}` configurado.

### B7 — La idempotencia se reclama antes de hacer el trabajo

En `tooling_webhook`, `store.claim(ingest_key)` va antes de `_persist_artifact` y
`pending.observe`; y `store.claim(review_key)` va antes de `enqueue`. Si cualquiera
de esos pasos falla (disco, Redis), la clave queda reclamada 7 días: el reintento
del CI recibe `"duplicado"` y el artefacto o la review se pierden en silencio.

**Arreglo:** liberar la clave si el paso falla (`DEL` en un `except`), o reclamar
al final.

### B8 — `twg` se lanza sin `timeout`

`subprocess.run` en `TwgVcsAdapter._run` y `check_twg` no lleva `timeout`. Un `twg`
colgado (red, Bitbucket lento) bloquea el worker indefinidamente. El heartbeat corre
en un hilo propio y sigue diciendo «sano», así que el contenedor no se reinicia.

**Arreglo:** `timeout=` configurable y traducir `TimeoutExpired` a `TwgError`.
Valorar que el heartbeat refleje también el progreso del bucle (p. ej. una marca de
«review en curso desde hace X» con un tope).

### B9 — Un fallo de Teams cuenta como fallo de la review

`publish` publica el resumen en el PR y después llama a `chat.notify`. Si Teams
falla, la excepción tumba el nodo y el job se reintenta; el reintento ve la marca
del resumen, sale antes y **la notificación de Teams no se envía nunca**.

**Arreglo:** notificar en un nodo aparte (P2) o, como mínimo, tratar Teams como
best-effort (`try/except` + log).

### B10 — El aviso de pausa nunca lleva enlace al PR

`_pr_url` lee `result.get("vcs")`, pero el VCS nunca está en el estado del grafo:
siempre devuelve `None` y la tarjeta de Teams sale sin el botón «Ver el PR».

**Arreglo:** pasar el `VcsPort` (o la URL ya resuelta) a `_on_paused`.

---

## 2. Robustez y seguridad

### R1 — Los fallbacks a memoria están abiertos por defecto

Si Redis no responde al construir el store:

- La **API** encola en una `InMemoryReviewQueue` que el worker no ve y responde
  202: la review se pierde.
- El **CLI** (`reviews decide`) imprime «Decisión encolada» y la decisión no llega a
  ningún sitio; `reviews list` dice «No hay reviews pausadas».
- El **checkpointer** cae a `InMemorySaver` si Postgres falla al arrancar: las
  pausas HITL no sobreviven a un reinicio.

**Propuesta:** memoria solo como opt-in explícito (p. ej. `REDIS_URL=memory://`,
`DATABASE_URL=` vacío); en cualquier otro caso, fallar de forma visible.

### R2 — Firma y token desactivados por defecto

`ENVIRONMENT` vale `development` por defecto y con `change-me` se desactivan tanto
la firma HMAC del webhook como el token Bearer de decisión. `docker-compose.yml`
expone el puerto 8000. Un despliegue que olvide `ENVIRONMENT=production` queda
abierto: cualquiera puede inyectar artefactos o aprobar/rechazar hallazgos.

**Propuesta:** invertir el default: exigir siempre firma y token salvo con un
`ALLOW_UNSIGNED=true` explícito. Valorar también protección anti-replay (timestamp
firmado).

### R3 — Conexión única a Postgres que no se recupera

`build_checkpointer` abre una sola `Connection`. Si Postgres se reinicia, todas las
operaciones fallan hasta reiniciar el worker (y el heartbeat no lo detecta). Ya
está la dependencia `psycopg[pool]`: usar `ConnectionPool` con `check`.

Además, `_thread_snapshot` se traga el error de lectura y devuelve `None`: el hilo se
trata como nuevo y una review **pausada** puede re-ejecutarse y avisar dos veces.
Mejor propagar la excepción y dejar que el job se reintente.

### R4 — `observe` no es atómico y guarda payloads enteros en Redis

`RedisPendingStore.observe` hace leer-modificar-escribir sin `WATCH`/Lua: dos
artefactos simultáneos de herramientas distintas pueden pisarse y la review nunca
llega a `READY` (se degrada por TTL). Además, `PendingReview.received` guarda el
payload completo (hasta 5 MB) durante 7 días, y `sweep` lo deserializa entero cada
30 s, aunque el artefacto ya está en disco.

**Propuesta:** un hash por review (`HSET reviews:pending:<key> <tool> 1`) + `HLEN`
contra las herramientas esperadas: atómico y sin payloads.

### R5 — Detalles de la API

- Cada request crea clientes Redis nuevos (con su `PING`) para cada store.
- Los handlers son `async` pero hacen I/O bloqueante (Redis, disco).
- El body se lee entero antes de comprobar el tamaño; conviene mirar
  `Content-Length` primero.
- Un `pr_id` no numérico produce un 500 en vez de un 400.
- `/profile/{repo_name}` ignora `repo_name`.

### R6 — Decisión parcial

`build_decision` acepta decidir solo un subconjunto de los pendientes. Al reanudar
no se vuelve a pausar, así que el resto se publica como si estuviera decidido (sin
línea de decisión). Decidir si se exige decidir todos o si los no decididos se
marcan explícitamente como «sin decisión».

---

## 3. Patrones que encajan

### P1 — Un solo *composition root*

Hay tres copias de `build_queue`, `build_paused` y `build_pending` (en
`api/main.py`, `worker/main.py` y `cli.py`) y ya han divergido: el `PausedStore` de
la API no recibe TTL, el del CLI no registra nada. Propuesta: un módulo
`infra/factories.py`, singletons creados en el `lifespan` de FastAPI e inyectados
con `Depends`. Ahí mismo se decide el backend (memoria explícita o fallo, ver R1).

### P2 — El checkpoint como unidad de reintento

Partir `publish` en `publish_inline → publish_summary → notify` y usar la
`RetryPolicy` de LangGraph por nodo para los fallos transitorios. Junto con
`invoke(None, config)` para hilos a medias (B2), un reintento continúa en el paso
que falló en vez de repetirlo todo. Arregla B2 y B9 y reduce el reencolado manual a
fallos no recuperables.

### P3 — Strategy / registro de parsers por herramienta

`ToolingReviewer` tiene `phpstan` escrito a mano, mientras que `expected_tools` y la
barrera de ingesta ya admiten varias herramientas (p. ej. `deptrac`). Un protocolo
`ToolParser` con una implementación por herramienta permite añadir herramientas sin
tocar el reviewer.

### P4 — Configuración tipada + Specification para la política

Validar `config/autonomy.yaml` y el perfil del repo con modelos Pydantic al
arrancar. Hoy una clave desconocida en `when` solo falla al decidir el primer
hallazgo (`PolicyError` en `_match_condition`), es decir, en mitad de una review.
Las reglas se pueden compilar a predicados combinables en lugar de la cadena
`if/elif` actual.

### P5 — DTOs de entrada en la API

Modelos Pydantic para los cuerpos (`ToolingWebhookIn`, `DecisionIn`) en lugar de
`body.get(...)` y casts a mano: validación y errores 422 coherentes gratis.

### P6 — Separar presentación del grafo

`graph/build.py` (650 líneas) mezcla el cableado del grafo con el render en markdown
y los tokens de dedupe. Llevar `_render_*`, `_footer` y los tokens a un módulo
`presentation/` deja el grafo legible y el render testeable por separado.

---

## 4. Limpieza menor

- `graph/build.py`: `_render_inline` usa `finding.id` para el token inline y la
  comprobación de `publish` usa `_finding_id(finding)`. Deben usar el mismo.
- `graph/build.py`: `_footer` y `_token_suffix` duplican `_summary_token`.
- `graph/build.py`: el comentario de `source_status` dice «Sin reducer», pero lo
  tiene (`_merge_status`).
- `domain/models.py`: `PENDING_ACTIONS` duplica `FindingAction.pending`.
- `domain/models.py`: `ReviewRequest.personas` no se usa en ningún sitio.
- `worker/main.py`: `build_runtime` devuelve `Any`, cuando ya existe `ReviewGraph`.
- `adapters/tooling_reviewer.py`: los ids `standards-…` se construyen con slugs
  truncados (40 y 24 caracteres) y pueden colisionar.
- `adapters/twg_vcs.py`: `list_review_markers` lee como mucho 200 comentarios; en
  PRs más largos el dedupe por marcas deja de ver las antiguas.

---

## 5. Huecos de tests

Los 227 tests cubren bien el camino feliz y la pausa/reanudación básica. Faltan
escenarios de fallo que sustentan los hallazgos de arriba:

- Reintento tras un fallo a mitad de `publish`, con y sin decisión humana (B2, B9).
- Envío a la DLQ de un job que lleva `decision` (B1).
- Pausa vencida cuyo resume falla de forma persistente (B3).
- Artefacto tardío sobre una review ya degradada de la misma ejecución (B4).
- Dos hallazgos con el mismo título en líneas distintas (B5).
- Webhook con un `repo` distinto del configurado (B6).
- Fallo de persistencia o de Redis después de reclamar la idempotencia (B7).
