Última modificación: 2026-10-23 | Creado: 2026-10-23

# Configuración desde el panel

> **Estado:** aprobada
>
> **Entrega objetivo:** el rol `operador` edita, en una página del panel, los diales
> que cambian una review. Cada control muestra el valor con el que el sistema trabaja
> hoy y tiene un botón que lo devuelve a ese valor inicial. Guardar publica un
> documento; la review que ya empezó sigue con el suyo.

---

## 1. Objetivo

Hoy esos diales viven en el entorno y en `Settings`. Cambiarlos es editar `.env` y
reiniciar procesos que no comparten memoria: `get_settings()` está cacheado en cada
uno. El panel ([019](../archivo/019-panel-de-operacion/spec.md)) ya distingue
`lector` y `operador`, pero no tiene dónde tocar un tope.

Se quiere:

1. Una página agrupada por secciones, con un texto de ayuda en cada control.
2. Que, sin haber guardado nada, cada control muestre el valor que el despliegue ya
   usa y la review se comporte igual que hoy.
3. Que cada control tenga **Restaurar**, y que ese botón devuelva el valor inicial
   de ese control.
4. Que un guardado sea una revisión entera, validada, y que la review en curso no
   cambie de valores a mitad.

## 2. No-objetivos

- **Secretos.** Tokens, claves y `PANEL_SESSION_SECRET` no se muestran ni se
  escriben. Siguen solo en el entorno ([ADR-0013](../../adr/0013-secretos-por-entorno-fail-closed.md)).
  La página no dice su valor; como mucho, en otro sitio, «configurado» o «falta».
- **Arranque.** URLs, puertos, `REDIS_URL`, `DATABASE_URL`, proveedor de VCS,
  motor, rutas de YAML, identidad del bot y `LOG_LEVEL` (se aplica en
  `logging.basicConfig` al arrancar) no tienen control.
- **Los tres ficheros de despliegue.** `config/autonomy.yaml`, `config/models.yaml`
  y el perfil del repo se siguen editando en git. El panel no es un segundo editor.
- **Proyección en Redis.** El `fix-worker` no tiene Postgres, pero ningún dial de
  esta página es suyo. Una copia en Redis sería otro sitio que puede mentir.
- **Aplicar el cambio dentro de una review ya empezada.**
- **Restaurar todos** con un solo botón. Cada control restaura el suyo.

## 3. Contexto y evidencia

`Settings` mezcla cuatro clases de campo. Tratarlas igual es lo que hace frágil un
formulario de «todas las variables»:

| Clase | Ejemplo | Quién la lee | Qué pasa si el panel la escribe |
|---|---|---|---|
| Arranque | `redis_url`, `panel_port` | El proceso, al vivir | El proceso viejo sigue con la vieja |
| Secreto | `github_token`, `llm_api_key` | El proceso que llama fuera | El panel pasa a ser un almacén de secretos |
| Fichero | `autonomy_config`, `models_config` | El worker, al arrancar | Dos editores del mismo sitio |
| Dial | `llm_budget_tokens_per_pr`, `review_tools_max_turns` | El worker, por review | Se puede fijar al empezar el job |

`.env.example` asigna los diales con su default (`LLM_BUDGET_TOKENS_PER_PR=2000000`,
`REVIEW_TOOLS_MAX_TURNS=4`, …). Si «el entorno gana siempre que la variable existe»,
un `.env` copiado de esa plantilla deja la página entera bloqueada. El candado tiene
que ser explícito, no la mera presencia de la variable.

El worker ya guarda el estado de la review en el checkpointer de Postgres
(`ReviewState` en `src/agent_review/graph/build.py`). Ese estado es el sitio donde
la foto de los diales sobrevive a un reintento del mismo hilo.

## 4. Decisiones

| # | Decisión | Motivo |
|---|---|---|
| CFG1 | El **valor inicial** de un control es el que `Settings` ya resolvió (entorno si la variable está puesta, si no el default del campo). No se copia a una fila al instalar | Sin filas, el sistema se comporta como hoy. Copiar los defaults a Postgres los congelaría y «restaurar» pelearía con un cambio posterior del código |
| CFG2 | La página **puede** sustituir ese valor. El entorno no bloquea el dial solo por estar en `.env` | Si no, la plantilla actual anula la página |
| CFG3 | `PANEL_CONFIG_LOCK` (lista de nombres de campo, vacía por defecto) es el candado. Un campo listado se muestra y no se edita | Producción puede clavar un tope sin bloquear el resto |
| CFG4 | **Restaurar** devuelve ese control al valor inicial dentro del formulario. No publica. **Guardar** publica el documento entero | La revisión es un documento. Un botón que escribiera una sola clave rompería el compare-and-swap y podría publicar otros campos a medias |
| CFG5 | El documento guardado solo contiene claves **distintas** del valor inicial. Restaurar y guardar esa clave la borra | Ausencia = valor de hoy. El documento no duplica `Settings` |
| CFG6 | Al empezar una review, el worker copia los overrides vigentes a `ReviewState` y el hilo usa esa foto. Un reanudado del checkpointer no relee la tabla. **Reintentar** desde el panel es una review nueva y lee la revisión publicada entonces | El job no cambia de presupuesto a mitad. La foto viaja en el estado que ya se persiste |
| CFG7 | Postgres es la fuente. Si la lectura falla y este proceso ya cargó una revisión, sigue con esa. Si no cargó ninguna, sigue con `Settings` y lo registra. No cae a defaults del código por debajo del entorno | Un Postgres caído no sube la autonomía ni enciende banderas. El worker sin Postgres ya no puede hacer checkpoint |
| CFG8 | Validar con el tipo del campo de `Settings`. Una clave desconocida, un secreto o un valor que el campo rechazaría se rechaza entero y sigue vigente la revisión anterior | El mismo modelo que ya falla al arrancar. No hay un segundo esquema |
| CFG9 | `max_autonomy_override` sigue pudiendo solo **bajar** el techo del YAML. El valor inicial es `""` (manda el YAML) salvo que el entorno traiga otro | No se abre por el panel un camino que el código ya cerró |
| CFG10 | El texto de ayuda vive en el metadato del campo, junto a la sección. La página lo pinta. `operacion.md` enlaza el catálogo y no copia la tabla | Dos redacciones del mismo dial se desfasarían |
| CFG11 | El número de esta iniciativa es **027**. El 026 se publicó con la bandeja de decisiones | Un número no se reutiliza |

## 5. Diseño o contrato

### 5.1 Catálogo

Cada dial declara sección, texto de ayuda y el default que ya tiene el campo. El
valor inicial que pinta la página es el de CFG1; la columna «Default del campo» es
el de hoy en `src/agent_review/settings.py`, que es el valor inicial cuando el
entorno no trae la variable.

**Presupuesto y lotes.** Lo que acota una pasada del revisor LLM.

| Campo | Default | Ayuda |
|---|---|---|
| `llm_max_findings` | `10` | Tope de hallazgos que una persona LLM puede emitir en la review. |
| `llm_budget_tokens_per_pr` | `2000000` | Tope de tokens de esa review, compartido por personas, fixer y verificador. Al llegar, lo que falte queda `budget_exhausted`. |
| `llm_batch_max_chars` | `20000` | Tamaño de un lote, en caracteres de diff. Un fichero que no cabe se trocea por hunks. |
| `llm_batch_retries` | `2` | Reintentos de un lote si el proveedor corta, cierra vacío o cierra sin JSON. Agotados, el lote sale `failed` y la review sigue. |
| `llm_context_max_chars` | `40000` | Tope del bloque de contexto (fichero completo y documentos del perfil) por lote. El diff no se recorta. |

**Verificador.** Topes del bucle L2. Si el YAML declara `severity_gate`, ese bloque
manda sobre `verifier_require_verified_for`.

| Campo | Default | Ayuda |
|---|---|---|
| `verifier_max_findings` | `5` | Cuántos hallazgos de la review pasa el verificador, los más graves primero. El resto queda `unverified`. |
| `verifier_max_tool_calls` | `8` | Llamadas a herramientas del verificador por hallazgo. |
| `verifier_max_context_bytes` | `60000` | Bytes de contexto que el verificador puede pedir por hallazgo. |
| `verifier_require_verified_for` | `P0,P1` | Severidades que exigen veredicto positivo. Si `autonomy.yaml` declara `severity_gate.require_verified_for`, manda el YAML. |

**Bucle de lectura.** Coste y forma de servir. Con estos defaults el comportamiento
servido es el de hoy. Ninguna bandera quita del *prompt* una capacidad anunciada.

| Campo | Default | Ayuda |
|---|---|---|
| `review_tools_roles` | vacío | Roles LLM que pueden leer y hacer `grep` mientras revisan. Vacío: una llamada por lote, sin herramientas. |
| `review_tools_max_turns` | `4` | Turnos con herramientas por lote, sin contar el turno final forzado. |
| `review_tools_max_calls` | `8` | Peticiones de herramientas por lote. |
| `review_tools_max_bytes` | `60000` | Bytes servidos por lote. |
| `review_tools_calls_per_turn` | `3` | Peticiones atendidas en un mismo turno. |
| `review_tools_grep_mode` | `literal` | Modo por defecto de `grep`: `literal` o `regex`. Un `regex` pedido explícito se atiende igual con el default `literal`. |
| `review_tools_read_outline` | `false` | Permite `mode="outline"` en `read_file`. Apagado, ese modo se sirve como `full` y se avisa. |
| `review_tools_read_state` | `false` | Incluye en el *prompt* el bloque de lecturas ya hechas en la review. |
| `review_tools_truncation_notice` | `true` | Al cortar un fichero, el aviso incluye el tamaño real. |
| `review_tools_strict_calls` | `false` | Una petición no atendida es un error. Apagado, se descarta con un aviso. |
| `review_tools_precache_context` | `true` | Precarga el contexto L1 en el bucle. Apagado, el modelo tiene que leerlo y gasta turnos. |
| `review_tools_io_cache` | `false` | Reutiliza lecturas de la misma review. Ahorra lectura del repo, no tokens del *prompt*. |

**Política.**

| Campo | Default | Ayuda |
|---|---|---|
| `max_autonomy_override` | vacío | Techo de autonomía por debajo del YAML. Vacío: manda `config/autonomy.yaml`. No puede subirlo. |
| `scope_ignore` | vacío | Patrones extra, separados por comas, de ficheros que el alcance ignora además de lockfiles, generados y vendor. |

Un campo nuevo entra en la página solo si declara sección y ayuda. Sin eso no aparece
y no se acepta en el documento.

### 5.2 Lo que ve el operador

Ruta nueva del servicio `web`, enlazada desde el panel. `lector` ve los valores y
los textos y no puede guardar. `operador` edita.

Cada control muestra:

- la etiqueta (el nombre del campo, tal cual);
- la ayuda del catálogo;
- el valor actual del formulario;
- **Restaurar**, deshabilitado cuando el formulario ya tiene el valor inicial o cuando
  `PANEL_CONFIG_LOCK` incluye el campo.

Un campo bloqueado se pinta con el valor inicial y el motivo («lo fija el
despliegue»). Restaurar no lo cambia.

La página dice la revisión publicada y, si hay una review en curso, la revisión que
esa review fijó. Así se distingue «guardado» de «ya en uso».

**Guardar** manda el formulario entero. El servidor calcula las claves distintas del
valor inicial, valida y, si el número de revisión que tenía el formulario no es el
publicado, responde conflicto y no escribe. El operador recarga. Dos guardados a la
vez: entra el primero.

Enteros por debajo de cero, un `review_tools_grep_mode` distinto de `literal` o
`regex`, o un `max_autonomy_override` por encima del YAML se rechazan con el nombre
del campo. La revisión anterior sigue publicada.

### 5.3 Persistencia

Una fila vigente en Postgres (overrides, número de revisión, actor, instante) y un
historial de revisiones con la foto de overrides de cada una. El historial es la
auditoría: quién, cuándo y qué claves cambiaron. No hay secretos en esa foto.

La primera instalación no inserta defaults. Cero revisiones significa CFG1 en todos
los controles.

### 5.4 La review

Al desencolar, el worker lee la fila vigente (CFG7) y escribe la foto en
`ReviewState` antes de construir personas y topes. El resto del grafo lee esa foto,
no `get_settings()`, para los campos del catálogo. Los demás campos siguen saliendo
de `get_settings()`.

`get_settings.cache_clear()` no es el mecanismo. Vaciar la caché cambia el proceso
entero y la review siguiente heredaría el valor de la anterior.

### 5.5 Límites de implementación

- **Siempre:** tests de esta spec en el mismo cambio que el código; `make docs-check`
  cuando se toque documentación; el metadato del campo como único texto de ayuda.
- **Preguntar antes:** una migración que no sea la tabla de esta página, una
  dependencia nueva, meter en el catálogo un campo de arranque o un secreto.
- **Nunca:** devolver un secreto en el HTML o en el JSON de la página; aceptar una
  clave que no esté en el catálogo; publicar un documento inválido; cambiar los
  diales de un hilo ya empezado.

### 5.6 Comandos

```
make test T=tests/test_panel_config.py
make ci
```

Los tests viven en `tests/test_panel_config.py` (resolución, restauración, candado,
conflicto de revisión y foto en el estado) y cubren el HTML en `tests/test_web_ui.py`.

## 6. Riesgos

| # | Riesgo | Qué lo contiene |
|---|---|---|
| R1 | Un operador baja el presupuesto a un número que deja la review en `budget_exhausted` | El valor inicial se restaura por control; la review en curso no cambia; el historial dice quién lo bajó |
| R2 | `PANEL_CONFIG_LOCK` mal escrito (un nombre que no es del catálogo) | Al arrancar el `web`, un nombre desconocido falla. No se ignora |
| R3 | El checkpointer de un hilo viejo no tiene el campo nuevo | Campo ausente = diales de `Settings`, que es el comportamiento de hoy |
| R4 | La ayuda del catálogo y `operacion.md` cuentan cosas distintas | CFG10: la página lee el metadato; la operación enlaza |

## 7. Criterios de hecho

1. Sin ninguna revisión guardada, una review usa los mismos números que antes de
   esta iniciativa, incluidos los que vengan del entorno.
2. Cada control del catálogo se pinta en su sección, con su ayuda y con Restaurar.
3. Restaurar deja ese control en su valor inicial y no modifica los demás ni escribe
   en Postgres.
4. Guardar un solo dial distinto publica una revisión que solo contiene esa clave.
   Restaurar ese control y guardar deja el documento sin esa clave, y el valor
   efectivo vuelve a ser el inicial.
5. Un campo de `PANEL_CONFIG_LOCK` no se puede cambiar ni restaurar a otro valor.
6. Un `lector` no puede guardar (`403`). Un documento inválido o con revisión
   vieja no sustituye al publicado.
7. Una review que ya fijó su foto no ve un guardado posterior. La siguiente sí.
8. `max_autonomy_override` no puede quedar por encima del techo del YAML.
9. El historial guarda actor y claves de cada revisión publicada.
10. `docs/operacion.md` explica la página y `PANEL_CONFIG_LOCK` sin copiar el
    catálogo. El término nuevo, si hace falta uno, entra en `CONTEXT.md`.

## 8. Preguntas abiertas

Ninguna que bloquee el plan. El catálogo de §5.1 es el de esta entrega; ampliarlo
es otra revisión de esta spec.
