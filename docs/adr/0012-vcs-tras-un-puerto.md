# ADR-0012 — El VCS vive tras un puerto; `twg` con cuenta de servicio y GitHub como alternativa

- **Estado:** Aceptado
- **Fecha:** 2026-10-11

## Contexto

El repo revisado está en Bitbucket. El CLI `twg` de Atlassian cubre Bitbucket y Jira.
El grafo no debe conocer flags de CLI ni endpoints, y GitHub tiene que poder
soportarse sin tocar el grafo.

## Decisión

- El grafo solo conoce `VcsPort` (`ports/vcs.py`): diff, diffstat, fichero en un ref,
  marcas publicadas, resolución de ancla, inline, comentario y tarea.
- **`TwgVcsAdapter`** (`VCS_PROVIDER=twg`): `subprocess` contra
  `twg bb … --output json --output-summary none` (`-o json` emite un envoltorio YAML,
  no JSON puro). El worker comprueba `twg --version` al arrancar.
- **Identidad de servicio.** API token *scoped* de un actor de servicio
  (`TWG_BBC_TOKEN` + `TWG_SITE`/`TWG_USER`) por entorno. No se usa el OAuth personal
  ni se monta `~/.config/twg`.
- **`GithubVcsAdapter`** (`VCS_PROVIDER=github`): API REST v3 con `httpx`. La
  resolución de ancla se hace en el cliente con `diff_parser.resolve_added_line`, el
  mismo criterio que comparten ambos. El diff se cachea por PR y las marcas se
  paginan.
- Un valor desconocido de `VCS_PROVIDER` falla al construir `Settings`.

## Consecuencias

- Los dos adaptadores ignoran el `repo` del job y usan el repo configurado. Por eso
  la API filtra el `repo` entrante ([ADR-0004](0004-ingesta-diferida-por-artefacto-del-ci.md)).
- `post_task` existe en el puerto pero el grafo no lo llama (en GitHub no hace nada).
- El paso siguiente en producción para GitHub es una GitHub App con tokens de
  instalación, no un PAT.

## Alternativas descartadas

- **OAuth personal montado.** Caduca en ~1 h, comenta a nombre de una persona y
  revocar a esa persona deja al bot ciego.
- **Exponer `twg` como MCP libre al LLM.** El LLM elige *qué* acción, nunca *cómo* se
  invoca la shell.
