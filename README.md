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
  los ancla a lineas reales del diff. No ejecuta la herramienta (Fase 3).
- `NullReviewer`: por defecto hasta cablear el perfil del repo.

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
