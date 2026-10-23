# ADR-0029 — Una review se lanza desde el panel con la URL del pull request

- **Estado:** Aceptado
- **Fecha:** 2026-10-23

## Contexto

La review solo arrancaba cuando el CI entregaba el artefacto
([ADR-0004](0004-ingesta-diferida-por-artefacto-del-ci.md)). Para probar un PR, o
revisarlo cuando el pipeline no ha disparado, hacía falta fabricar ese webhook.
El panel ya encola comandos y no tiene credencial de VCS
([ADR-0020](0020-panel-como-servicio-aparte.md)): darle el token para leer el
commit le daría también, si reutilizara el adaptador, la capacidad de comentar.

La identidad de la ejecución es `repo:pr:sha:pipeline`
([ADR-0005](0005-identidad-de-la-ejecucion.md)). Sin el SHA no hay hilo, y un
identificador nuevo en cada clic publicaría dos veces el mismo commit.

## Decisión

- El operador pega la URL en el panel. El panel **solo la parsea** (GitHub
  `/pull/N`, Bitbucket `/pull-requests/N`) y encola un `ReviewRequest` con
  `launch=True`, `head_sha` vacío y `pipeline_id` `panel`.
- Comprueba que `owner/repo` sea el del despliegue (`Settings.configured_repo`).
  Para eso el servicio `web` recibe el nombre del repo, **no** el token.
- El worker, que ya habla con el VCS, lee el commit **antes** de `graph.invoke`.
  Ese SHA es el hilo. Un reintento no vuelve a leerlo: un push posterior no
  cambia la ejecución que ya empezó.
- Un segundo lanzamiento del mismo commit reutiliza el hilo y no publica otra
  vez. El pipeline del CI sobre ese mismo commit sigue siendo otra ejecución.
- El lanzamiento **no** espera la barrera de ingesta. Si el artefacto aún no
  está, esa parte de la review sale incompleta, igual que una pasada degradada.

## Consecuencias

- Cancelar el job mientras sigue en la cola marca la clave sin SHA
  (`repo:pr::panel`). El worker la mira antes de llamar al VCS.
- Panel y worker tienen que desplegarse juntos: un worker anterior ignoraría
  `launch` y ejecutaría el grafo con el SHA vacío.
- Dos resúmenes en el mismo PR son posibles si el CI y el panel revisan el
  mismo commit. Es la regla de identidad que ya había, no una excepción.

## Alternativas descartadas

- **Resolver el SHA en el panel.** Obligaba a darle la credencial de VCS y
  rompía el aislamiento del servicio.
- **Un `pipeline_id` nuevo en cada clic.** Un doble clic publicaba dos reviews
  del mismo commit.
- **Pasar el lanzamiento por la barrera de ingesta.** El operador no vería la
  review hasta que llegara el artefacto, o hasta el TTL. Quien pulsa «Lanzar»
  pide la review ahora.
- **Reutilizar el `pipeline_id` del CI.** La URL no lo trae, y atar el
  lanzamiento a un pipeline desconocido chocaría con una review que el CI aún
  no ha encolado.
