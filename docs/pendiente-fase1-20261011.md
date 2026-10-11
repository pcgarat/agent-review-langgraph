Última modificación: 2026-10-11 | Creado: 2026-10-11

# Qué falta para cerrar de verdad la Fase 1

Auditoría honesta del estado de la Fase 1 (report-only) tras implementar los
Slices A, B y C. No es el plan (`tasks/plan.md`) sino el **delta** entre «el código
existe y pasa tests» y «produce reviews útiles en producción sin sorpresas».

Leyenda de prioridad:

- **P0 — Bloqueante:** sin esto la Fase 1 no entrega valor real.
- **P1 — Robustez:** funciona, pero falla de forma silenciosa o se rompe en cuanto
  hay volumen/errores.
- **P2 — Calidad de señal:** afecta a si el equipo *quiere* leer la review.
- **P3 — Deuda/higiene:** no bloquea, pero conviene no arrastrarlo.

---

## Resumen ejecutivo

Lo que **ya está cerrado y verificado**: el clamp `report` (ningún `fix_*`), el
contrato de `Finding`, la cola API↔worker, la ingesta diferida con barrera y TTL,
el `ToolingReviewer` (estática), la persona LLM `correctness`, el publish
inline+resumen+Teams con tope y prioridad, y la retirada del webhook de PR. 113
tests verdes, `ruff` limpio.

Lo que **falta**: un cambio que vive **fuera de este repo** (que el CI suba el
artefacto), la verificación con `twg` real (smoke Nivel 2), y una capa de
robustez/dedupe que hoy no existe. Hay además tres piezas de código muerto
(Postgres, `get_diffstat`, `max_files_per_pr`) y dos fuentes de verdad que pueden
divergir (`expected_tools`).

> **La verdad incómoda:** con el CI actual de `emotivoo-v2`, el agente **no recibe
> artefactos**, así que toda review sale degradada por TTL. Hoy el sistema es
> correcto pero **mudo en la práctica** hasta que el `POST` del CI esté desplegado.

---

## A. Bloqueantes (P0)

### A1. El CI del repo revisado debe hacer `POST /webhook/tooling`

Es *el* interruptor de la Fase 1 y el único trabajo que no vive en este repo.
Detalle en `docs/ingesta-diferida-ci-20261011.md` §7.1.

- [ ] El step de PHPStan de `emotivoo-v2` envía el JSON crudo al agente (firmado
      con `X-Reviews-Signature`), además de (o en vez de) subirlo a Bitbucket.
- [ ] `TOOLING_WEBHOOK_SECRET` (variable secured de Bitbucket) == secreto del agente.
- [ ] `REVIEWS_URL` alcanzable desde el runner.
- [ ] El `POST` **no** bloquea el pipeline (`|| true`): si el agente cae, el PR
      sigue su curso.
- [ ] Rama `chore/publicar-salida-phpstan-para-code-review` mergeada.

**Verificación:** un PR real genera `{"status":"aceptado"}` en la API y
`Review completada` en el worker, en vez de `Review INCOMPLETA`.

### A2. Arranque reproducible (`bin/twg` + `.env`)

El `Dockerfile` copia `bin/twg` **si existe**; hoy `bin/` solo tiene un README, así
que el contenedor arranca **sin `twg`** y no puede leer el diff. Y no hay `.env`.

- [ ] Decidir cómo se provee `twg` al contenedor: copiar el binario (frágil, versión
      no fijada) o **instalarlo de forma reproducible** (versión pinneada). Hoy el
      propio `Dockerfile` lo admite: *«su instalación no es reproducible todavía»*.
- [ ] Documentar/automatizar la creación de `.env` con los secretos reales.
- [ ] Validar `twg --version` dentro del contenedor en el arranque (fallar ruidoso
      si falta, en vez de descubrirlo al procesar la primera review).

### A3. Smoke Nivel 2 ejecutado una vez

El doc `docs/smoke-fase1-20261011.md` está escrito, pero **nadie ha corrido el
Nivel 2**. Hasta hacerlo no sabemos si el contrato de `twg` 1.3.5 publica inlines
con el formato que esperamos.

- [ ] Elegir un PR real y ejecutar §2.1–§2.3 del doc de smoke.
- [ ] Verificar inline por hallazgo + resumen + un único teaser de Teams.
- [ ] Guardar la evidencia (IDs de comentario o captura) en `docs/`.

---

## B. Robustez del pipeline (P1)

### B1. Dedupe contra comentarios previos del agente (re-push / re-run)

**El hueco más importante del runtime.** El publish no consulta los comentarios ya
publicados: si el mismo commit se revisa dos veces (dos `pipeline_id` distintos,
que el diseño considera «review nueva») o si un job se reprocesa, el PR recibe
**inlines duplicados**. El dedupe por `(fichero, título)` solo actúa **dentro** de
una ejecución.

- [ ] Marcar los comentarios del agente (firma/emoji/cabecera reconocible).
- [ ] Antes de publicar, leer los comentarios existentes (`twg bb prs get --full`
      o `prs activity`) y saltar los hallazgos ya reportados.
- [ ] Test con un FakeVcs que ya trae un comentario previo del agente.

> Nota crítica: el dedupe entre `standards` y `correctness` del nodo `merge` es
> **nominal**. Dos modelos casi nunca coinciden en el texto del título, así que en
> la práctica no deduplica nada de valor. El dedupe que importa es *este* (contra
> lo ya publicado), no el del merge.

### B2. Cola sin ack, sin reintentos y sin DLQ

`process_request` hace `BLPOP` (el job **ya salió** de la cola), y ante cualquier
excepción solo registra el log. El job **se pierde**: no hay reintento ni dead
letter. El propio plan lo aceptaba para Fase 1, pero un fallo transitorio de `twg`
o de Teams pasa de «un reintento» a «review que nunca ocurrió».

- [ ] Decidir: reintento con contador en Redis (simple) o aceptar pérdida con log
      que incluya la clave para reencolar a mano.
- [ ] Si se acepta la pérdida, dejar el `review_key` en el log de forma explícita
      para poder reinyectarlo.

### B3. Procesamiento secuencial (un solo worker)

Un PR grande (PHPStan con decenas de hallazgos → hasta 20 `diff-line` + 20 inlines
+ resumen + Teams) bloquea la cola durante toda su duración: cada `diff-line` es un
`subprocess` a `twg`. No hay pool ni paralelismo.

- [ ] Medir el tiempo real en el smoke Nivel 2.
- [ ] Si supera unos segundos, paralelizar por `pr_id` (varios consumers) o acotar
      el número de `diff-line`.

### B4. `expected_tools` tiene dos fuentes que pueden divergir

La **barrera** de ingesta se construye desde `Settings.expected_tools` (variable de
entorno), pero el **perfil del repo** también declara `expected_tools: [phpstan]`, y
la activación de la persona estática depende de `checks.typecheck` del perfil. Si
alguien añade una herramienta al perfil y se olvida del `.env` (o al revés), la
barrera espera un conjunto distinto al que el perfil declara.

- [ ] Una sola fuente de verdad: o el perfil manda (y el env solo lo sobreescribe
      explícitamente), o el env manda y el perfil deja de declararlo.
- [ ] Test que falle si divergen.

### B5. Scope sin filtro de ruido ni tope de ficheros

`max_files_per_pr: 10` está declarado en `autonomy.yaml` y **no se aplica en
ninguna parte**. `get_diffstat` está implementado y **nunca se usa**. No se excluyen
`composer.lock`, `var/**`, generados, etc. El plan lo pedía en T5 («Scope excluye
rutas configuradas») y quedó a medias.

- [ ] Filtrar ficheros ruidosos antes de pasar el diff a las personas.
- [ ] Aplicar (o retirar) `max_files_per_pr` con un comportamiento definido
      (¿truncar? ¿avisar? ¿saltar la review?).

### B6. Publicación no atómica

Si `post_comment` (resumen) falla **después** de publicar los inlines, quedan
inlines sin resumen. No hay orden «resumen primero» ni compensación.

- [ ] Decidir el orden y qué hacer si falla a mitad (¿abortar? ¿continuar?).

### B7. Sin watchdog ni healthchecks de servicio

`run_forever` no está supervisado y el `docker-compose.yml` **no** declara
`restart:` para `api`/`worker`, ni healthcheck para ellos (solo `postgres`). Si el
worker muere, se queda muerto.

- [ ] `restart: unless-stopped` (o similar) en `api` y `worker`.
- [ ] Healthcheck del worker (p.ej. latido en Redis).

---

## C. Calidad de la señal (P2)

### C1. Revisión humana del formato inline (checkpoint B abierto)

La casilla «Formato inline revisado por humano» sigue desmarcada. El inline es el
entregable visible: si el texto no convence, la Fase 1 falla de producto aunque
todos los tests pasen.

- [ ] Revisar un hilo inline real (del smoke) y ajustar plantilla
      (`_render_inline`) y resumen (`_render_summary`).
- [ ] Decidir mención a autor, idioma (ES/EN) y longitud máxima.

### C2. Un único tipo de anclaje; borrados puros sin inline

`resolve_inline_anchor` ancla con la primera evidencia; si falla, el hallazgo cae
al resumen. No se usa `from_line` para líneas borradas ni hay estrategia para
borrados puros (queda documentado como guardrail, no implementado).

- [ ] Definir qué hacer con hallazgos en líneas eliminadas.

### C3. LLM sin medición de falsos positivos ni corte de presupuesto

`budget_tokens_per_pr` **solo se loguea**; no corta ni limita. No hay ninguna
métrica de aciertos: el equipo no puede saber si `correctness` aporta o solo mete
ruido.

- [ ] Definir cómo se mide (¿ratio de hallazgos aceptados? ¿comentarios
      resueltos?). Sin esto, la Fase 2 (memoria/ajuste) no tiene datos.
- [ ] Implementar corte real del presupuesto, no solo log.

### C4. Tema protegido inferido por heurística débil

Compartido con la estática: la lista de pistas (`auth`, `password`, `sql`…) es
mínima y puede marcar de más o de menos. En Fase 1 da igual (todo es `report`), pero
en Fase 3 decide el autofix.

- [ ] Revisar la lista con el equipo de seguridad antes de dar permisos.

---

## D. Configuración, contrato y decisiones abiertas (P2/P3)

### D1. Clamp `report` en YAML vs override por deploy

Hoy está en `config/autonomy.yaml` (visible, versionado). Si un despliegue quiere
`fix_with_approval`, no hay override por settings.

- [ ] Decidir: ¿clamp en el YAML (explícito, cambia con el repo) o flag de
      settings que solo puede *bajar* el techo? (decisión abierta #4 del plan).

### D2. Plantilla del artefacto acoplada por convención

`tooling_output_template` vive en `Settings`, **no** en el perfil, y debe coincidir
con la ruta que publica el CI «por convención». Un cambio en el CI rompe la lectura
sin que nada avise.

- [ ] Moverlo al perfil o validar en el arranque que la ruta resuelve.

### D3. Sin límite de tamaño en el `POST` de tooling

El doc de ingesta (§6) pedía rechazar payloads > N MB con 413. `_persist_artifact`
escribe sin comprobar tamaño.

- [ ] Añadir límite y respuesta 413.

### D4. Sin rate-limit ni autenticación de origen en `/webhook/tooling`

Solo firma HMAC. Si el secreto está en `change-me`, la firma se **omite**
(`_verify_signature` retorna sin validar). Riesgo en deploy si el secreto no se fija.

- [ ] Fallar el arranque en producción si el secreto sigue en `change-me`.

---

## E. Deuda e higiene (P3)

- [ ] **Postgres/checkpointer cableado sin usar.** `pyproject` y `compose` montan
      Postgres «checkpointer y auditoría», pero **ningún código lo usa** (no hay
      `psycopg` importado en `src/`). O se etiqueta como reservado para Fase 2, o se
      retira del compose hasta entonces.
- [ ] **`get_diffstat` sin uso** y **`max_files_per_pr` sin aplicar** (ver B5).
- [ ] **Sin CI propio.** Este repo no tiene `.github/workflows`, pipeline ni
      `Makefile`: `uv run pytest`/`ruff`/`mypy` son manuales. Para «dejarlo bien»
      conviene un CI que corra los tres.
- [ ] **`mypy` con 10 errores preexistentes.** No son de esta fase, pero el repo se
      declara `strict = true`; conviene limpiarlos para que un error nuevo no se
      pierda entre el ruido.
- [ ] **Observabilidad mínima.** Sin trazas (LangSmith mencionado en el diseño §10,
      no implementado), sin métricas de coste/FP, sin correlación por `review_key`
      más allá del log.
- [ ] **Tests de `TeamsWebhookAdapter`** solo cubren «no falla sin webhook»; con
      webhook real, nada (requiere mock HTTP).

---

## Orden recomendado para cerrar

1. **A1 + A2** (habilitan que ocurra algo real) → **A3** (ver que ocurre bien).
2. **B1** (dedupe re-push) antes de invitar a más gente: el ruido de duplicados es
   lo que mata la confianza en un bot de review.
3. **B7 + B2** (que no se muera y que no pierda jobs en silencio).
4. **C1** (formato humano) — es barato y decide la adopción.
5. **B4/B5/D1–D3** (fuentes de verdad y límites), antes de escalar a más repos.
6. **E** cuando haya CI verde propio.

## Definición de hecho de la Fase 1 (reafirmada)

- Hallazgos **anclados inline** en el diff + resumen en el PR + teaser en Teams.
- **Cero** escritura de código en el repo del PR (sin push/commit/branch).
- Ninguna `Decision.action` es `fix_*`.
- Una review real, disparada por el `POST` del CI, publicada y **revisada por una
  persona** que diga «esto lo leería».

Con A1–A3 y C1 en verde se puede declarar la Fase 1 hecha y pasar a decidir la
Fase 2 (HITL, Opción B).
