# reviews

Agente autonomo de code review sobre LangGraph. Recibe webhooks de Bitbucket, revisa el
diff de un PR, decide por hallazgo (informar, escalar, arreglar) y publica en Microsoft
Teams y en el propio PR.

Este README cubre como levantarlo y como funciona. La documentacion de fondo:

| Documento | Contenido |
|---|---|
| `docs/diseno-agente-code-review-20261010.md` | Diseno completo: arquitectura, autonomia, HITL, fases, perfil de `emotivoo-v2` |
| `docs/ingesta-diferida-ci-20261011.md` | Por que la review espera al CI y por que el artefacto es un puente desechable |
| `docs/decisiones-publish-inline-20261011.md` | Formato inline, orden de publicacion y borrados |
| `docs/smoke-fase1-20261011.md` | Smoke E2E (Nivel 1 local y Nivel 2 contra Bitbucket) |
| `docs/pendiente-fase1-20261011.md` | Registro de la auditoria de cierre y su resolucion |
| `tasks/plan.md` | Estado del plan: tareas cerradas y pendientes humanos |

## Arranque

```bash
scripts/bootstrap-env.sh          # crea .env desde .env.example y comprueba dependencias
cp ~/.local/bin/twg bin/twg       # el contenedor lo usa como adaptador de Bitbucket/Jira
docker compose up --build
```

`bootstrap-env.sh` es idempotente: no sobreescribe un `.env` existente, lo deja con
permisos `600` y resume qué secretos faltan por rellenar sin imprimir sus valores.
El worker valida `twg --version` en el arranque y falla ruidoso si falta (en vez de
descubrirlo al procesar la primera review).

**Credencial de `twg` (cuenta de servicio).** El agente no usa el `auth.conf` de una
persona: recibe un **API token scoped de un actor de servicio** por entorno
(`TWG_BBC_TOKEN`, más `TWG_SITE`/`TWG_USER`), vía `.env`. Ventajas: no caduca cada
hora como el OAuth, su identidad es del bot (no tuya) y se revoca sin tocar a nadie.
El contenedor **no monta** `~/.config/twg`.

- `api`  — FastAPI, expone `POST /webhook/tooling`, `GET /healthz`, `/pending` y `/profile/{repo}`
- `worker` — ejecuta el grafo LangGraph con checkpointer durable (Postgres)
- `postgres` — checkpointer y auditoria
- `redis` — cola de eventos e idempotencia

`api` y `worker` arrancan con `restart: unless-stopped` y healthcheck (el del
worker es un latido en `/tmp/reviews-worker-heartbeat`): si el worker se cuelga, se
re-arranca solo.

## Flujo

1. El CI de `emotivoo-v2` corre PHPStan y hace `POST /webhook/tooling` con el JSON de
   salida (firmado en `X-Reviews-Signature`).
2. La `api` valida la firma, deposita el artefacto, y registra el artefacto recibido en
   el registro de ingesta (`PendingReview`). Si es el ultimo que faltaba, encola la review.
3. El `worker` consume, construye el grafo y ejecuta.
4. Segun la politica de `config/autonomy.yaml`, cada hallazgo se informa, escala o corrige.

> **Por que no dispara el PR.** El webhook de PR llegaba *antes* de que el CI produjese el
> artefacto, asi que la review se ejecutaba sin datos (`0 hallazgos` enganoso). La ingesta
> diferida encola la review **solo cuando el artefacto que necesita ya esta dentro**. El
> detalle del diseno esta en `docs/ingesta-diferida-ci-20261011.md`.

### No re-publicar la misma review (dedupe)

Un reinicio del worker, la reentrega de un mensaje o un `pipeline_id` nuevo sobre el
mismo commit no deben duplicar los comentarios del PR. Hay dos capas:

1. **Checkpointer durable** (Postgres): el grafo se invoca con `thread_id =
   review_key` (commit + `pipeline_id`). Si el hilo ya tiene estado y no hay nodos
   pendientes, el worker **no vuelve a ejecutar** el grafo. Es tambien la base del
   `interrupt` de la Fase 2 (HITL).
2. **Marca de publicacion**: el resumen general lleva un pie
   (`<!-- reviews:<hash-de-la-ejecucion>:ok -->`, o `:partial` si la review fue
   incompleta) derivado de la **ejecucion** (`review_key = commit + pipeline_id`),
   no solo del commit. Antes de publicar, el `publish` lee los comentarios del PR:
   - Si **esta ejecucion** ya dejo su marca con la misma completitud (o una
     completa), no re-publica. Un reintento del mismo `pipeline_id` es idempotente.
   - Un `pipeline_id` **nuevo** sobre el mismo `sha` es una review nueva y publica
     la suya (antes «ganaba la primera marca por commit» y la nueva quedaba en
     silencio).
   - Un inline ya publicado no se repite aunque el resumen hubiera fallado. Una
     review **degradada** (`:partial`) **no** bloquea a una completa posterior
     (ni de otro pipeline, ni de la misma ejecucion si el artefacto llega tarde).

> Esto alinea el dedupe con el `thread_id` (una ejecucion, una marca) y cierra Q1.

### Fallos transitorios (reintento y DLQ)

Un fallo de `twg` o de Teams no debe perder la review. `process_request` propaga la
excepcion y el worker reencola el mismo job con un contador de intentos
(`MAX_REVIEW_ATTEMPTS`, 3 por defecto). Agotados los intentos, el job **no se
pierde en el log**: va a una **DLQ** (`reviews:jobs:dead`) con su `review_key`,
intentos y motivo, desde donde se puede reinyectar o inspeccionar. Las reviews
**incompletas no se reencolan**: la ingesta diferida ya espero al artefacto (ver
`.env.example`), asi que re-ejecutar solo duplicaria la publicacion.

### Scope y coste

Antes de las personas, el nodo `scope` descarta ficheros ruidosos (lockfiles,
`var/**`, `vendor/**`, generados; ampliable con `SCOPE_IGNORE`) y, si
`max_files_per_pr` esta declarado en `autonomy.yaml`, recorta el diff a los
ficheros con mas churn. Asi un PR con un `composer.lock` enorme no gasta tokens ni
inlines en ruido. El presupuesto de la persona LLM (`budget_tokens_per_pr`) **corta**
la llamada si el coste estimado lo supera, no solo la loguea.

Las decisiones abiertas de formato de publicacion (orden, borrados puros, formato
inline) estan fijadas en `docs/decisiones-publish-inline-20261011.md`.

## Estado actual

**Fase 1 (report-only) implementada.** La ingesta diferida encola en Redis al
completarse el artefacto del CI, el worker ejecuta el grafo y publica **comentarios
inline** (+ resumen en el PR + Teams). Política en `max_autonomy: report`: cero
escritura de código en el repo del PR. Lo pendiente es verificación humana
(`tasks/plan.md`).

Personas de review (puerto `reviews.ports.reviewer`):

- `ToolingReviewer` (`standards`): convierte la salida de PHPStan en hallazgos y
  los ancla a lineas reales del diff. No ejecuta la herramienta. Activa por
  defecto cuando el perfil declara `checks.typecheck`.
- `LlmReviewer` (`correctness`): propone hallazgos con un modelo y los valida
  contra el contrato; una linea inventada por el modelo se descarta. **Opt-in**:
  requiere `LLM_REVIEWER_ENABLED=true` y `LLM_MODEL` (ver `.env.example`). El
  proveedor es un puerto (`LlmPort`); el adaptador incluido habla el contrato
  OpenAI `/chat/completions`, asi que vale OpenAI, Azure, Groq, vLLM u Ollama.
- `NullReviewer`: por defecto cuando el perfil no declara checks y no hay LLM.

Ambas personas emiten el mismo contrato `Finding`; el nodo `merge` deduplica por
`(fichero, titulo)`, de modo que un mismo problema detectado por estatica y por
LLM aparece **una sola vez**.

**De donde sale la salida de PHPStan (Fase 1).** El agente **no** ejecuta la
herramienta: eso es el sandbox de la Fase 3. La salida la deposita el pipeline de
CI como artefacto y el `ToolingReviewer` la lee a traves del puerto
`ToolingOutputSource`. El `worker` monta `FileToolingOutputSource` con la
plantilla `TOOLING_OUTPUT_TEMPLATE` (por defecto
`var/tooling/{pr_id}/{head_sha}/{tool}.json`). Cambiar a «ejecutar en sandbox» en
Fase 3 es otro adaptador, sin tocar reviewer ni grafo: el diseno converge ambos
casos en un puerto `CheckRunner`/`ExecResult`
(`docs/ingesta-diferida-ci-20261011.md` §4). El artefacto del CI es un **puente
desechable**, no la cimentacion: con PHPStan y Deptrac bloqueantes y en verde, su
salida aporta cero senal nueva.

La fuente distingue **analisis limpio** de **salida no leida**: `read()` devuelve
el estado (`present` / `missing` / `unreadable`) junto al contenido, y la persona
lo propaga hasta el `publish`. Como el CI sube el artefacto con `capture-on:
always`, un PHPStan que corre **siempre** deja artefacto, incluso sin errores: si
falta, es que el analisis no ocurrio. Por eso, cuando el perfil declara
`checks.typecheck` y la salida no se encuentra, la review **no esta completa**: el
PR recibe un aviso explicito de "Review incompleta" («su ausencia de hallazgos no
equivale a un PR limpio»), el `ReviewOutcome` marca `complete == False` y el
worker registra un `warning`. Un `head_sha` vacio, en cambio, es un error de
resolucion de ruta y se registra como fallo.

El productor del artefacto es el CI del repo revisado. En `emotivoo-v2` el step
de PHPStan vuelca el JSON a `var/tooling/<pr_id>/<commit>/phpstan.json` y lo sube
con `capture-on: always` (se publica aunque PHPStan encuentre errores), que es la
ruta que resuelve la plantilla por defecto. `BITBUCKET_COMMIT` en un build de PR
es el commit *source* head, el mismo `head_sha` que envia el webhook.

El contrato de hallazgo vive en `contracts/findings-schema.json`.

## Smoke real contra Bitbucket

Requiere una credencial de `twg` valida (`TWG_BBC_TOKEN`) y un PR con cambios:

```bash
REVIEWS_LIVE_PR=417 uv run pytest tests/test_live_twg.py -m live
```

Es solo lectura (no publica comentarios). Los tests `live` estan excluidos por defecto.

Para el **Nivel 2** completo (levanta el stack, firma y deposita el artefacto como
el CI, verifica inline + resumen + Teams):

```bash
scripts/smoke-nivel2.sh 417            # PR real; requiere TWG_BBC_TOKEN y .env
```

## Desarrollo

```bash
make install     # uv sync con extras dev
make ci          # ruff (lint+formato) + mypy + pytest, igual que el CI
```

El CI propio (`.github/workflows/ci.yml`) corre `ruff check`, `ruff format
--check`, `mypy` y `pytest` en cada push y PR. `mypy` esta limpio (strict) y
`pytest` en verde.
