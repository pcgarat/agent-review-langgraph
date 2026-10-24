# ADR-0034 — Olvidar una ejecución borra el estado local y deja los comentarios

- **Estado:** Aceptado
- **Fecha:** 2026-10-24

## Contexto

Un segundo lanzamiento del mismo commit reutiliza el hilo y no vuelve a publicar
([ADR-0029](0029-lanzamiento-desde-el-panel.md)). Con el motor Claude, un artefacto
ya válido tampoco vuelve a llamar al CLI. Para repetir una ejecución que falló, o
una que se quiere volver a ver desde cero, no bastaba «Reintentar»: ese comando
sigue el hilo que ya existe y, si la review está cancelada, solo retira la marca.

El panel no puede borrar el hilo. Es el único escritor del grafo
([ADR-0020](0020-panel-como-servicio-aparte.md)). Tampoco tiene credencial de VCS,
así que no puede quitar comentarios del pull request.

## Decisión

- El operador pulsa **Eliminar** en la ficha. El panel sube un contador
  `reviews:generation:<review_key>`, retira de la cola, el parking y la DLQ las
  copias de esa ejecución, y encola un `ReviewRequest` con `forget=True`.
- El worker, al tomarlo, no invoca el grafo. Borra los hilos `review_key` y
  `review_key:partial`, el artefacto de Claude de ese `repo + pr + head_sha`, el
  inbox de pausa, la marca de cancelación, el histórico, el log y el razonamiento.
- Un job de esa ejecución con generación menor no corre. Un lanzamiento nuevo del
  panel, que aún no trae SHA, adopta la generación vigente al resolver el commit.
- La transcripción del CLI la retira el panel, que es quien monta el volumen. El
  checkout de `claude-runner` se reutiliza.
- Los comentarios ya publicados en el pull request se quedan. La marca de dedupe
  sigue en ellos: un hallazgo ya publicado no se vuelve a escribir.

## Consecuencias

- Hay que desplegar panel y worker juntos. Un worker que ignore `forget` trataría
  el job como una review normal.
- Eliminar mientras el worker está dentro de esa review espera a que el job en
  curso termine; el borrado va detrás y limpia lo que ese job haya escrito. El
  lanzamiento que se encole después del borrado corre con la generación nueva.
- Un job rezagado (la reanudación de un fix, una decisión encolada antes) lleva la
  generación vieja y se descarta.

## Alternativas descartadas

- **Borrar el hilo desde el panel.** Segundo escritor sobre el checkpointer, que
  es la alternativa que ya descartó ADR-0020.
- **Un `pipeline_id` nuevo en cada borrado.** Publicaría otra ejecución del mismo
  commit y duplicaría el resumen. La identidad sigue siendo la de ADR-0005.
- **Quitar los comentarios del PR.** El panel no tiene token de VCS, y borrar
  comentarios ajenos al marcador de esta ejecución no es parte del comando.
