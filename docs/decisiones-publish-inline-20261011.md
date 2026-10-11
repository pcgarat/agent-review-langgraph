Última modificación: 2026-10-11 | Creado: 2026-10-11

# Decisiones de publicación: orden, borrados y formato inline

Fija tres decisiones de producto del nodo `publish` que no eran automatizables:
el orden de publicación y su compensación (B6), el tratamiento de hallazgos en
líneas borradas (C2) y el formato inline (C1, pendiente de visto bueno humano).

Complementa `docs/diseno-agente-code-review-20261010.md` §6.1.1.

---

## B6 — Publicación no atómica: orden y compensación

Si `post_comment` (resumen) falla después de publicar los inlines, quedan inlines
sin resumen. ¿Abortar o continuar?

**Decisión: resumen al final, y el reintento completa lo que falta.** Ya está
resuelto por el diseño de marca (`:ok`/`:partial`) más el checkpointer.

El orden es deliberado y no se cambia:

1. Se publican los **inlines** (uno por hallazgo anclado).
2. Se publica **el resumen**, que es quien lleva la marca invisible
   `<!-- <token>:ok|:partial -->`.
3. La marca va **solo en el resumen** porque es la **última** escritura: si el
   proceso muere entre el paso 1 y el 2, el resumen no llegó a escribir la marca,
   así que el reintento ve la review como **no publicada** y:
   - no repite los inlines ya escritos (los reconoce por su marca por hallazgo
     `<!-- <token>:i:<8 base32> -->` leída del PR), y
   - escribe el resumen que faltaba.

Esto se fija con tests (`test_reintento_tras_fallo_del_resumen_no_duplica_el_inline`
y `test_reintento_con_checkpointer_completa_sin_duplicar_el_inline`). La
alternativa «resumen primero» rompería la propiedad: una marca `:ok` sin inlines
haría que un reintento **no** completase nada.

**Compensación:** no se compensa (no se borran inlines huérfanos). Es preferible
un inline sin resumen, que se recupera en el reintento, a borrar comentarios que
una persona ya podría estar leyendo. El único caso irrecoverable (agotar los
reintentos) acaba en la DLQ (B2) y no en silencio.

---

## C2 — Borrados puros: sin inline, al resumen

¿Qué hacer con hallazgos en líneas eliminadas?

**Decisión: un hallazgo en una línea borrada cae al resumen; nunca se ancla con
`--from-line` de forma automática.**

Motivo: Bitbucket ancla el lado nuevo (`--line`) de forma estable, pero el lado
viejo (`--from-line`) exige un número de la revisión *anterior* que `twg prs
diff-line` no garantiza de forma inequívoca en todos los casos. Un ancla mal
resuelta publica el comentario en la línea equivocada, que es peor que no anclarlo.

- Los revisores (`ToolingReviewer`, `LlmReviewer`) anclan **solo** en líneas
  añadidas; un hallazgo cuya evidencia apunta a una línea borrada no encuentra
  ancla y va al resumen con su `fichero:linea`, sin perderse.
- `VcsPort.post_inline_comment` ya acepta `from_line` para cuando en Fase 3 el
  sandbox pueda resolver borrados con contexto fiable; hoy **no se usa**.
- Está alineado con el diseño §5: *«Sin inline en borrados puros: usar
  `--from-line` solo si `diff-line` lo indica»*.

**Reevaluar** cuando exista un caso real de PR donde el borrado sea el hallazgo
principal y el resumen no baste. Hoy no hay evidencia de que anclar borrados
aporte más que ruido.

---

## C1 — Formato inline: preparado para revisión humana

Revisar un hilo inline real y ajustar plantilla y resumen; decidir mención a
autor, idioma y longitud.

**Decisión de partida (pendiente del visto bueno humano):**

| Aspecto | Propuesta | Motivo |
|---|---|---|
| Idioma | **ES** en el texto, **EN** en rutas/símbolos | El canal del equipo está en español; el código no se traduce |
| Mención al autor | **No** mencionar por defecto | Evita notificar a alguien por cada hallazgo; el PR ya notifica |
| Longitud inline | Cabecera + `why_it_matters` + `sugerencia` opcional | El detalle largo no cabe en un hilo del diff |
| Longitud resumen | Índice con conteo y `fichero:linea` | Es el mapa, no el detalle |
| Estado de política | Se muestra la acción aplicada y, en `report`, la razón | En Fase 1 todo es `report`; se informa *qué habría hecho* |

### Ejemplo renderizado (real, del PR 430)

Inline (un hilo por hallazgo). Como el titulo es el mensaje de la herramienta
truncado, la cabecera usa el mensaje **completo** y no se repite en el cuerpo:

```markdown
**[P3] Rolling back this migration recreates UNIQ_PROGRAM_USER_PORTAL_USER but cannot restore the participants that the 1:1 unique index discarded on the way up**

_Accion: report_ · confianza 100

_Politica: Ruta protegida (migrations/Version20261008140000.php): requiere aprobacion_

<!-- reviews:5ed90c15838b:i:e4ezailw -->
```

Resumen (un comentario general):

```markdown
## Code review automatica

4 hallazgos · 4 inline · 0 solo en resumen

- **[P3] PortalUser::$programUser is an inverse OneToOne ...** (`src/.../PortalUser.php:70`) · report · inline
- **[P3] Rolling back this migration ...** (`migrations/...:39`) · report · inline

<!-- reviews:5ed90c15838b:ok -->
```

Reglas de ruido que ya aplica el render:

- El detalle (`why_it_matters`) **no** se repite en el resumen si el hallazgo ya
  tiene inline; solo aparece en los que caen al resumen.
- La linea `_Politica:_` se **omite** cuando la razon es la generica ("Regla de
  politica"); se conserva cuando el guard aporta (p. ej. `never_auto_paths`).
- La marca de dedupe (`<!-- reviews:...:i:<8> -->`) se acorta a base32 y va al
  pie: Bitbucket pinta el comentario HTML, asi que cuanto mas corta, menos ruido.

> Nota abierta: Bitbucket **no oculta** los comentarios HTML. Si se encuentra una
> forma fiable de ocultar la marca, sustituye al pie visible.

Cuando este ejemplo te valga, marca **P3** de `tasks/todo.md` («Formato inline
revisado por humano»); si quieres cambiar algo (idioma, mención al autor, tono),
se ajusta `_render_inline` / `_render_summary` en `src/reviews/graph/build.py`.

---

## Q1 — Qué marca una review ya publicada: la ejecución, no el commit

**El problema.** El dedupe tenía dos llaves que no coincidían:

- El hilo del checkpointer (`thread_id`) era la **ejecución** (`review_key = commit
  + pipeline_id`), con el docstring explícito de que «un `pipeline_id` nuevo sobre
  el mismo `sha` es una review nueva (intencional)».
- La marca embebida (`dedupe_token`) se derivaba del **commit** (`idempotency_key`).

Consecuencia: la primera review que tocaba un commit dejaba `:ok` y **toda** review
posterior de un pipeline nuevo sobre ese mismo `sha` quedaba muda («ya hay una
review completa publicada para este commit»). La intención de «review nueva»
chocaba con «gana la primera marca». Además, una ejecución que publicaba un aviso
`:partial` **bloqueaba** su propia review completa posterior: la segunda pasada veía
la marca `:partial` y el early-return solo miraba `:ok`… sin llegar a escribir el
`:ok` (la marca del resumen se decidía con el `complete` de esa pasada, pero el
camino quedaba emponzoñado por el aviso ya escrito).

**Decisión: la marca se ancla a la ejecución (`review_key`), igual que el hilo.**

- `dedupe_token = reviews:<sha256(review_key)[:12]>`. El `sha` sigue dentro de la
  clave, así que commits distintos no colisionan; añadir el `pipeline_id` solo
  separa re-ejecuciones sobre el mismo commit.
- El `publish` se salta la publicación **solo si esta misma ejecución** ya dejó su
  marca con la **misma completitud** (o una completa, que no se degrada). No hay
  herencia entre ejecuciones distintas: cada `pipeline_id` publica la suya.

**Consecuencias (todas cubiertas por tests):**

| Escenario | Antes | Ahora |
|---|---|---|
| Reintento del mismo `pipeline_id` | no re-publica | no re-publica (idempotente) |
| `pipeline_id` nuevo, mismo `sha` | **no** publicaba | **publica** su review |
| Ejecución degradada (`:partial`) → completa con el **mismo** `pipeline_id` | quedaba bloqueada en silencio | publica y **sustituye** el aviso |
| Reintento de la **misma** ejecución degradada | volvía a escribir el aviso | no lo repite (`:partial` idéntico) |
| Commit distinto | marca distinta | marca distinta |

Tests: `test_publish_no_repite_la_misma_ejecucion`,
`test_pipeline_nuevo_sobre_el_mismo_commit_publica_su_review`,
`test_reintento_de_la_misma_ejecucion_degradada_no_repite_el_aviso`,
`test_una_ejecucion_degradada_no_bloquea_su_completa_con_mismo_pipeline`,
`test_dedupe_token_se_ancla_a_la_ejecucion_no_al_commit` (`tests/test_graph.py`).

> **Revisión de `main` (B4):** la fila «degradada → completa con el mismo
> `pipeline_id`» se cumplía en el `publish`, pero no llegaba a ejecutarse en el
> worker: las dos pasadas compartían hilo y la completa veía el hilo ya terminado.
> Ahora la pasada degradada corre en su propio hilo (`thread_id` con sufijo
> `:partial`). Test: `test_b4_artefacto_tardio_en_la_misma_ejecucion_publica_la_review_completa`
> (`tests/test_revision_main.py`).
>
> **Revisión de `main` (B5):** la marca inline se calcula con `_finding_id`, el mismo
> id que comprueba `publish`. Antes usaba `finding.id`, y con un id vacío todos los
> inline compartían marca: el segundo se daba por publicado.

**Riesgo que se acepta.** Un re-push **sin** `pipeline_id` (misma clave que la
ejecución anterior) no se distingue de un reintento: es el precio de que «una
ejecución = una marca». Hoy el `pipeline_id` siempre viaja (`BITBUCKET_BUILD_NUMBER`
en el `POST`), así que el caso es teórico; si algún día falta, el peor efecto es no
re-publicar, nunca duplicar.

> **Nota para Fase 2:** al abrir el `interrupt` del HITL, la reanudación llega desde
> otro proceso sobre el **mismo** `thread_id`, que es justo la ejecución dueña de la
> marca. Esa coherencia (hilo == marca) es la que evita re-evaluar y re-publicar al
> reanudar.
