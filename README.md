# reviews

Agente autonomo de code review sobre LangGraph. Recibe webhooks de Bitbucket, revisa el
diff de un PR, decide por hallazgo (informar, escalar, arreglar) y publica en Microsoft
Teams y en el propio PR.

El diseno completo esta en `docs/`. Este README solo cubre como levantarlo.

## Arranque

```bash
cp .env.example .env    # rellena BITBUCKET_WEBHOOK_SECRET y TEAMS_WEBHOOK_URL
cp ~/.local/bin/twg bin/twg   # el contenedor lo usa como adaptador de Bitbucket/Jira
docker compose up --build
```

- `api`  — FastAPI, expone `POST /webhook/bitbucket` y `GET /healthz`
- `worker` — ejecuta el grafo LangGraph
- `postgres` — checkpointer y auditoria
- `redis` — cola de eventos e idempotencia

## Flujo

1. Bitbucket hace POST al webhook en cada PR abierto o push.
2. La `api` calcula la clave `repo:pr:head_sha`, la marca en Redis (idempotencia) y encola.
3. El `worker` consume, construye el grafo y ejecuta.
4. Segun la politica de `config/autonomy.yaml`, cada hallazgo se informa, escala o corrige.

## Estado actual

Fase 1 en curso: webhook encola en Redis, el worker ejecuta el grafo y publica
**comentarios inline** (+ resumen en el PR + Teams). Politica en
`max_autonomy: report`.

Personas de review (puerto `reviews.ports.reviewer`):

- `ToolingReviewer` (`standards`): convierte la salida de PHPStan en hallazgos y
  los ancla a lineas reales del diff. No ejecuta la herramienta.
- `NullReviewer`: por defecto cuando el perfil no declara checks.

**De donde sale la salida de PHPStan (Fase 1).** El agente **no** ejecuta la
herramienta: eso es el sandbox de la Fase 3. La salida la deposita el pipeline de
CI como artefacto y el `ToolingReviewer` la lee a traves del puerto
`ToolingOutputSource`. El `worker` monta `FileToolingOutputSource` con la
plantilla `TOOLING_OUTPUT_TEMPLATE` (por defecto
`var/tooling/{pr_id}/{head_sha}/{tool}.json`). Cambiar a «ejecutar en sandbox» en
Fase 3 es otro adaptador de ese puerto, sin tocar reviewer ni grafo.

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

> Nota para el smoke (T9): `twg` no expone descarga de artefactos de pipeline
> (solo logs). El agente deberia leerlos por la API de Bitbucket; hoy `reviews` los
> espera ya presentes en disco (`./var`, montado en el worker).

El contrato de hallazgo vive en `contracts/findings-schema.json`.

## Smoke real contra Bitbucket

Requiere `twg login` y un PR con cambios:

```bash
REVIEWS_LIVE_PR=417 uv run pytest tests/test_live_twg.py -m live
```

Es solo lectura (no publica comentarios). Los tests `live` estan excluidos por defecto.

## Desarrollo

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest
uv run ruff check src
```
