# ADR-0005 — Ejecución, hilo y marca de dedupe comparten identidad

- **Estado:** Aceptado
- **Fecha:** 2026-10-11

## Contexto

El dedupe tenía dos llaves que no coincidían. El hilo del checkpointer era la
**ejecución** (`commit + pipeline_id`). La marca embebida en el PR era el **commit**.
Resultado: la primera review de un commit dejaba `:ok` y cualquier pipeline posterior
sobre ese commit quedaba mudo. Además, una pasada `:partial` bloqueaba su propia
review completa.

## Decisión

- **Una ejecución es `review_key = repo:pr:sha:pipeline_id`.** Un `pipeline_id` nuevo
  sobre el mismo commit es una review nueva y publica la suya.
- `thread_id = review_key`, salvo la **pasada degradada**, que usa
  `review_key:partial`. Así, si el artefacto llega tarde, la pasada completa tiene un
  hilo nuevo y no se encuentra con uno ya terminado.
- `dedupe_token = reviews:<sha256(review_key)[:12]>`. Va en la marca de resumen
  (`:ok` / `:partial`) y en la marca inline (`:i:<8 base32 del finding_id>`).
- `publish` se salta la publicación solo si **esta** ejecución ya dejó una marca de
  la misma completitud, o una completa. Una completa sustituye a una degradada; una
  degradada nunca sustituye a una completa.

## Consecuencias

| Escenario | Resultado |
|---|---|
| Reintento del mismo `pipeline_id` | No vuelve a publicar |
| `pipeline_id` nuevo, mismo commit | Publica su review |
| Degradada → artefacto tardío, mismo `pipeline_id` | Publica la completa en el hilo nuevo; los inline ya publicados no se repiten |
| Reintento de una ejecución degradada | No repite el aviso |
| Commit distinto | Marca distinta |

- En la reanudación HITL, hilo y marca son la misma ejecución. Por eso reanudar no
  reevalúa ni vuelve a publicar.
- **Riesgo aceptado:** un re-push sin `pipeline_id` no se distingue de un reintento.
  Lo peor que pasa es que no se vuelve a publicar; nunca se duplica. Hoy el CI siempre
  manda `BITBUCKET_BUILD_NUMBER`.
- La dedupe sobrevive a la poda de checkpoints porque vive en las marcas del PR.
- Bitbucket muestra los comentarios HTML, así que la marca se ve. Por eso es corta y
  va al pie.

## Alternativas descartadas

- **Marca por commit.** Silenciaba pipelines legítimos.
- **Un solo hilo para la pasada degradada y la completa.** La completa encontraba el
  hilo terminado y no hacía nada.
