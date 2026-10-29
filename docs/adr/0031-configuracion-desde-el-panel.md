# ADR-0031 — Los diales de una review se publican desde el panel

- **Estado:** Aceptado
- **Fecha:** 2026-10-23
- **Corrección:** 2026-10-24 — los techos del CLI (`claude_max_turns`,
  `claude_timeout_s`) entran en el catálogo. El runner no lee Postgres: el
  worker se los manda en `POST /run` cuando la foto de la review los trae.

## Contexto

Los topes de una review (presupuesto, lotes, bucle de lectura, techo de autonomía)
viven en el entorno. `get_settings()` está cacheado en cada proceso, y
`build_runtime` cierra esos números al construir el grafo. Cambiarlos es editar
`.env` y reiniciar. El panel ya distingue `lector` y `operador`, pero no tiene
dónde tocar un dial.

[ADR-0013](0013-secretos-por-entorno-fail-closed.md) fija que los secretos solo
entran por el entorno. Meterlos en el panel convertiría la sesión en un almacén
de tokens.

`.env.example` asigna los diales con su default. Si el entorno ganara por el
mero hecho de estar definido, un `.env` copiado de esa plantilla dejaría la
página bloqueada.

## Decisión

- El catálogo es un `Dial` (sección y ayuda) en el `metadata` del campo de
  `Settings`. Un `Secret` no puede llevarlo. La página pinta ese texto; no hay
  una segunda redacción.
- El valor inicial de un control es el que `Settings` ya resolvió. El documento
  guardado solo contiene las claves que se apartan de ese valor. **Restaurar**
  devuelve el control a ese valor en el formulario y no escribe.
  **Guardar** publica el documento entero, con compare-and-swap.
- Postgres (`panel_config` y `panel_config_history`) es la fuente. Sin
  `DATABASE_URL`, el almacén es en memoria y empieza vacío. Si una lectura
  falla y el proceso ya tenía una revisión, sigue con esa.
- `PANEL_CONFIG_LOCK` lista los diales que el despliegue no deja cambiar. Un
  nombre que no esté en el catálogo aborta el arranque del `web` y del worker.
- Al empezar una review, el worker monta revisores, política, presupuesto y
  grafo con la foto elegida, y la escribe en `ReviewState.dial_snapshot`. Cola,
  checkpointer, VCS y el registro de modelos se construyen una vez. Un hilo ya
  empezado usa la foto de su checkpoint. **Reintentar** desde el panel vuelve a
  leer lo publicado.
- `max_autonomy_override` no puede quedar por encima del techo del YAML. El
  motor ya lo ignoraba; la publicación lo rechaza.

## Consecuencias

- Sin ninguna revisión guardada, una review usa los mismos números que antes.
- La review en curso no cambia de diales porque alguien guarde. La siguiente,
  sí. Un reintento del panel, sí.
- Los ficheros `config/autonomy.yaml`, `config/models.yaml` y el perfil del
  repo siguen siendo de despliegue. El panel no los edita.
- Montar el grafo por review reutiliza el checkpointer y el registro. No abre
  un pool nuevo por job.

## Alternativas descartadas

- **Escribir el `.env` desde el panel.** El proceso que ya arrancó no lo ve, y
  el fichero no es de un solo servicio.
- **Que el entorno gane siempre que la variable exista.** La plantilla actual
  anula la página.
- **Vaciar `get_settings.cache_clear()`.** Cambia el proceso entero y la review
  siguiente hereda el valor de la anterior.
- **Proyectar el documento en Redis.** El `fix-worker` no lee estos diales. Una
  copia más es otro sitio que puede mentir.
- **Reconstruir el grafo una sola vez al arrancar y leer los diales dentro de
  cada nodo.** Los revisores, el presupuesto y la política cierran los números
  en el constructor. Hacer que todos los lean del estado es un refactor más
  grande que montar esa parte por review.
