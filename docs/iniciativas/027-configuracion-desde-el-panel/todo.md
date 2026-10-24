Última modificación: 2026-10-24 | Creado: 2026-10-23

# Todo: Configuración desde el panel

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md). **W va antes que U**: la página no se pinta hasta que una
review de prueba usa la foto.

## Bloque C — Contrato en memoria (checkpoint C-K)

- [x] **C1** Catálogo junto al campo
  - Cada dial de la spec §5.1 declara sección y ayuda en el metadato de
    `Settings`. Un campo sin eso no entra. Un `Secret` no puede declarar sección.
  - `PANEL_CONFIG_LOCK` es un campo de arranque (lista de nombres, vacía por
    defecto). No es un dial.
  - Verifica: `tests/test_panel_config.py` recorre el catálogo y falla si falta
    un campo de §5.1, si sobra uno, o si un secreto está dentro.
  - Ficheros: `src/agent_review/settings.py`, `tests/test_panel_config.py`.

- [x] **C2** Valor inicial, override y rechazo
  - Valor inicial = `Settings` ya resuelto. El documento solo guarda claves
    distintas de ese valor. Restaurar una clave es quitarla del documento.
  - Se rechazan clave desconocida, tipo inválido, entero negativo,
    `review_tools_grep_mode` distinto de `literal`/`regex`, y
    `max_autonomy_override` por encima del YAML.
  - Un nombre de `PANEL_CONFIG_LOCK` fuera del catálogo es un error al construir
    el candado, no un aviso.
  - Verifica: `tests/test_panel_config.py`.
  - Ficheros: módulo nuevo del contrato (junto a `settings.py`),
    `tests/test_panel_config.py`.

- [x] **C3** ADR
  - Un ADR aceptado con el número libre en ese momento: Postgres como fuente,
    foto por review, candado explícito, y por qué el entorno no bloquea un dial
    solo por estar en `.env`.
  - Verifica: `make docs-check`.
  - Ficheros: `docs/adr/NNNN-configuracion-desde-el-panel.md`, `docs/adr/README.md`.

### Checkpoint C-K

- [x] Sin documento, el valor efectivo es el de `Settings`.
- [x] Clave ajena, secreto, entero negativo y techo de autonomía ilegal se rechazan.
- [x] `make test T=tests/test_panel_config.py` en verde.

## Bloque P — Persistencia (checkpoint P-K)

- [x] **P1** Almacén
  - `CREATE TABLE IF NOT EXISTS` al arrancar `web` y `worker`, con la misma
    `DATABASE_URL` del checkpointer. Sin URL, almacén en memoria vacío.
  - Fila vigente (overrides, revisión, actor, instante) e historial con la foto
    de cada revisión.
  - Verifica: `tests/test_panel_config.py` con el almacén en memoria y, si el
    test de checkpointer ya tiene Postgres de mentira, un caso de crear la tabla
    dos veces.
  - Ficheros: módulo del almacén, arranque de `web/main.py` y `worker/main.py`
    solo el gancho, `tests/test_panel_config.py`.

- [x] **P2** Compare-and-swap y última foto buena
  - Guardar con la revisión que ya no es la vigente no escribe.
  - Dos guardados concurrentes de la misma revisión: uno entra.
  - Si la lectura falla y el proceso ya tenía una revisión, sigue con esa. Si
    no tenía ninguna, sigue con `Settings`.
  - Verifica: `tests/test_panel_config.py`.
  - Ficheros: módulo del almacén, `tests/test_panel_config.py`.

### Checkpoint P-K

- [x] El historial guarda actor y claves.
- [x] Una revisión vieja no sustituye a la publicada.
- [x] `make test T=tests/test_panel_config.py` en verde.

## Bloque W — La review (checkpoint W-K)

- [x] **W1** Montar por review sin otro pool
  - `run_forever` construye una vez cola, checkpointer, VCS, chat, progreso,
    registro y cancelación. Por review monta revisores, política, presupuesto
    y grafo con la foto.
  - La foto se escribe en `ReviewState` al empezar. El presupuesto Redis de esa
    review usa el total de la foto.
  - Un `graph=` inyectado no consulta el almacén.
  - Verifica: `tests/test_worker.py` (un override de `llm_max_findings` o del
    presupuesto llega al grafo de esa review y no a la construcción del proceso).
  - Ficheros: `src/agent_review/worker/main.py`, `src/agent_review/graph/build.py`,
    `tests/test_worker.py`.

- [x] **W2** Reanudado y reintento
  - Al continuar un hilo, la foto sale del checkpoint. Sin el campo, se monta
    con `Settings`.
  - Reintentar desde el panel lee la fila vigente, no la foto del hilo viejo.
  - Verifica: `tests/test_worker.py`.
  - Ficheros: `src/agent_review/worker/main.py`, `tests/test_worker.py`.

### Checkpoint W-K

- [x] La review nueva usa el override; el hilo reanudado no; el reintento sí.
- [x] `make test T=tests/test_worker.py` en verde.

## Bloque U — Página (checkpoint U-K)

- [x] **U1** Controles
  - Ruta del `web`, enlazada desde el panel. Una sección por grupo del catálogo.
  - Cada control: nombre del campo, ayuda, valor y **Restaurar**. Restaurar
    copia `data-initial` al control y no llama al servidor. Deshabilitado si ya
    está en el valor inicial o si el candado incluye el campo.
  - `lector` ve la página y no ve Guardar.
  - Verifica: `tests/test_web_ui.py` (23 controles, cuatro secciones, el botón
    y el atributo del valor inicial).
  - Ficheros: `src/agent_review/web/ui.py`, `tests/test_web_ui.py`.

- [x] **U2** Guardar
  - `fetch` JSON con `require_command_session`. El cuerpo es el formulario
    entero más la revisión que tenía la página.
  - `lector` → 403. Revisión vieja → conflicto y la fila no cambia. Valor
    inválido → el nombre del campo y la revisión anterior sigue vigente.
  - La página muestra la revisión publicada y, si hay una review en curso, la
    que esa review fijó en W1.
  - Verifica: `tests/test_web_ui.py` y `tests/test_panel_config.py`.
  - Ficheros: `src/agent_review/web/ui.py`, `src/agent_review/web/commands.py`
    solo si el POST vive junto a los otros comandos, `tests/test_web_ui.py`.

### Checkpoint U-K

- [x] Restaurar no escribe. Guardar una sola clave distinta publica solo esa clave.
- [x] `make test T=tests/test_web_ui.py` en verde.

## Bloque D — Documentación (checkpoint D-K)

- [x] **D1** Operación y entorno
  - `docs/operacion.md`: la página, el rol, Restaurar frente a Guardar, y
    `PANEL_CONFIG_LOCK`. Enlaza el catálogo de la spec; no lo copia.
  - `.env.example`: `PANEL_CONFIG_LOCK` comentado, vacío.
  - `CONTEXT.md` solo si hace falta un término que el resto del repo vaya a usar.
  - `docs/arquitectura.md` §5: la tabla nueva, una fila.
  - Verifica: `make docs-check` y `make ci`.
  - Ficheros: `docs/operacion.md`, `.env.example`, `docs/arquitectura.md`,
    `CONTEXT.md` si aplica.

### Checkpoint D-K

- [x] `make ci` en verde.
- [x] Los criterios de hecho 1–10 de la spec tienen un test o una frase en
  `operacion.md` que los cubre.

## Bloque R — Revisión del código (checkpoint R-K)

Hallazgos de la revisión de `feat/configuracion-desde-el-panel` (024).

- [x] **R1** Candado y formulario
  - Un dial bloqueado se pinta con su valor inicial, no con el override publicado
    que `apply_document` descarta; el navegador no lo envía y el servidor acepta
    que falte. Un valor distinto sigue siendo `400`.
  - Verifica: `tests/test_web_ui.py` y `tests/test_panel_config.py`.
  - Ficheros: `src/agent_review/web/ui.py`, `src/agent_review/panel_config.py`.

- [x] **R2** Valor inicial pintado
  - El formulario viaja con sus `data-initial`; el servidor difunde contra esos
    valores y responde `409` si no coinciden con los del proceso.
  - Un entero vacío es `400` con su campo, nunca `0`.
  - Verifica: `tests/test_web_ui.py`, `tests/test_panel_config.py`.
  - Ficheros: `src/agent_review/web/ui.py`, `src/agent_review/panel_config.py`.

- [x] **R3** Techo y arranque
  - `MAX_AUTONOMY_OVERRIDE` se valida con `PolicyEngine.from_file`, y solo cuando el
    valor cambia: el resto de guardados no depende de leer el YAML.
  - El `web` monta `config/` (solo lectura) y recibe los mismos diales que el
    worker. El candado y el almacén se resuelven en el `lifespan`.
  - Verifica: `tests/test_compose.py`, `tests/test_panel_config.py`.
  - Ficheros: `src/agent_review/web/ui.py`, `src/agent_review/web/main.py`,
    `docker-compose.yml`.

- [x] **R4** Almacén
  - `publish` devuelve lo escrito (no relee), la primera inserción se serializa con
    un cerrojo consultivo y `DATABASE_URL` vacía falla cerrado en producción.
  - El esquema se crea sentencia a sentencia: psycopg prepara cada `execute` y una
    sentencia preparada no admite dos `CREATE TABLE`.
  - Verifica: `tests/test_config_store_postgres.py` (se salta sin `PANEL_CONFIG_TEST_DATABASE_URL`).
  - Ficheros: `src/agent_review/config_store.py`, `tests/test_config_store_postgres.py`.

- [x] **R5** Worker
  - Montar el grafo por review pasa por el mismo camino de error que el trabajo: un
    fallo reencola o va a la DLQ, nunca mata el bucle.
  - Un reencolado interno no conserva `repin_dials`; el fallback sin almacén deja
    rastro con la `review_key`.
  - Verifica: `tests/test_worker.py`, `tests/test_panel_config.py`.
  - Ficheros: `src/agent_review/worker/main.py`.

- [x] **R6** El `web` resuelve los mismos diales que el worker
  - Compose interpola al `web`, del mismo `.env`, cada dial del catálogo,
    `PANEL_CONFIG_LOCK` y `AUTONOMY_CONFIG`; antes solo recibía dos diales y el
    candado no le llegaba.
  - Verifica: `tests/test_compose.py` recorre `catalog()`.
  - Ficheros: `docker-compose.yml`, `tests/test_compose.py`, `docs/operacion.md`.

- [x] **R7** Reintento del panel que no monta
  - Un fallo al montar reencola conservando `repin_dials`: montar no toca el hilo.
    Un reencolado tras invocar lo sigue quitando.
  - `run_forever` delega en `_mount_or_requeue`, que se prueba con un
    checkpointer caído.
  - Verifica: `tests/test_worker.py`.
  - Ficheros: `src/agent_review/worker/main.py`, `tests/test_worker.py`.

- [x] **R8** Formulario de otra versión
  - Un dial añadido o retirado con la página abierta es `409` (recargar), no
    `400` «falta en el formulario».
  - Verifica: `tests/test_panel_config.py`.
  - Ficheros: `src/agent_review/panel_config.py`.

- [x] **R9** Etiquetas de ADR tras renumerar
  - Las referencias al lanzamiento desde el panel vuelven a decir ADR-0029.
  - Ficheros: `CONTEXT.md`, `docs/arquitectura.md`, `docs/operacion.md`,
    `docker-compose.yml`.

### Checkpoint R-K

- [x] `make ci` en verde.
- [x] Los endpoints `/config` y `/api/config` están en `docs/operacion.md` §5.
