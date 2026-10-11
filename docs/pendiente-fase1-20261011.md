Última modificación: 2026-10-11 | Creado: 2026-10-11

# Qué falta para cerrar de verdad la Fase 1

Auditoría honesta del estado de la Fase 1 (report-only) tras implementar los
Slices A, B y C. No es el plan (`tasks/plan.md`) sino el **delta** entre «el código
existe y pasa tests» y «produce reviews útiles en producción sin sorpresas».

> **Estado 2026-10-11 (cierre de ronda):** todos los puntos accionables dentro del
> repo están resueltos (A2, B2, B4–B7, C3, C4, D1–D4 y la higiene E). `make ci` en
> verde: `ruff` limpio, `mypy` strict limpio, 166 tests. Lo que queda **no es
> código**: A1 (POST del CI en el repo revisado), A3 (ejecutar el smoke Nivel 2 con
> `twg` y secretos reales), B1 (dedupe contra comentarios previos), B3 (medir
> latencia en vivo) y C1 (revisión humana del formato inline). Son tareas humanas o
> externas, no bloqueantes de código. El detalle de cada una está abajo.

Leyenda de prioridad:

- **P0 — Bloqueante:** sin esto la Fase 1 no entrega valor real.
- **P1 — Robustez:** funciona, pero falla de forma silenciosa o se rompe en cuanto
  hay volumen/errores.
- **P2 — Calidad de señal:** afecta a si el equipo *quiere* leer la review.
- **P3 — Deuda/higiene:** no bloquea, pero conviene no arrastrarlo.

---

## Resumen ejecutivo

Lo que **ya está cerrado y verificado**: el clamp `report` (ningún `fix_*`), el
contrato de `Finding`, la cola API↔worker con reintentos y **DLQ**, la ingesta
diferida con barrera y TTL, el `ToolingReviewer` (estática), la persona LLM
`correctness` con **corte de presupuesto**, el publish inline+resumen+Teams con tope
y prioridad, la retirada del webhook de PR, el filtro de ruido de scope y
`max_files_per_pr` (usa `get_diffstat`), el arranque reproducible con validación de
`twg` y secretos, y las decisiones de formato de publicación documentadas.

Lo que **falta** ya **no es código**: la verificación con `twg` real (smoke Nivel 2),
la revisión humana del formato inline y el dedupe contra comentarios ya publicados
(B1), que es la mejora de robustez de más valor pendiente pero no bloquea la Fase 1.

> **La verdad incómoda:** con el CI actual de `emotivoo-v2` el agente **no recibe
> artefactos**, así que toda review sale degradada por TTL. Pero eso ya **no**
> invalida la Fase 1 (`docs/ingesta-vs-ejecucion-20261011.md`): es un puente, y el
> valor real está en las personas LLM y los checks que no corren en CI.

---

## A. Bloqueantes (P0)

> **Revisión 2026-10-11:** A1 **ya no es un bloqueante de la Fase 1**. La decisión
> está en `docs/ingesta-vs-ejecucion-20261011.md`: el artefacto del CI es un puente
> desechable, y con `make stan`/`make deptrac` bloqueantes y en verde aporta cero
> señal nueva. Si el `POST` del CI llega, se usa; si no, la Fase 1 sigue entregando
> valor vía personas LLM y checks que no corren en CI.

### A1. El CI del repo revisado debe hacer `POST /webhook/tooling` — *nice to have*

Mejora el modo «artefacto», no lo habilita: sin él, la review sale degradada por
TTL y se pierde la anotación inline de un pipeline rojo. Detalle en
`docs/ingesta-diferida-ci-20261011.md` §7.1.

- [ ] El step de PHPStan de `emotivoo-v2` envía el JSON crudo al agente (firmado
      con `X-Reviews-Signature`), además de (o en vez de) subirlo a Bitbucket.
- [ ] `TOOLING_WEBHOOK_SECRET` (variable secured de Bitbucket) == secreto del agente.
- [ ] `REVIEWS_URL` alcanzable desde el runner.
- [ ] El `POST` **no** bloquea el pipeline (`|| true`): si el agente cae, el PR
      sigue su curso.
- [ ] Rama `chore/publicar-salida-phpstan-para-code-review` mergeada.

**Verificación:** un PR real genera `{"status":"aceptado"}` en la API y
`Review completada` en el worker, en vez de `Review INCOMPLETA`.

### A2. Arranque reproducible (`bin/twg` + `.env`) — ✅ Resuelto (2026-10-11)

El `Dockerfile` copiaba `bin/twg` **si existía**; el contenedor podía arrancar
**sin `twg`** y fallaba al leer el primer diff. No había forma de preparar `.env`.

**Solución:** `scripts/bootstrap-env.sh` (idempotente) crea `.env` desde
`.env.example`, comprueba `docker`/`compose`/`uv`/`twg` y avisa de secretos sin
imprimirlos. El worker y la API validan `twg --version` en el arranque
(`check_twg`, `REQUIRE_TWG`): si falta, fallan ruidoso en vez de en la primera
review. El `Dockerfile` admite `--build-arg REQUIRE_TWG=true` para validarlo en
build. La versión de `twg` se documenta como pinneada por despliegue (sigue sin ser
una instalación reproducible desde cero, pero ya no falla en silencio).

- [x] Arranque documentado y automatizado (`scripts/bootstrap-env.sh`).
- [x] Validación de `twg --version` en el arranque (falla ruidoso si falta).
- [x] `.env` generado desde `.env.example` con los nuevos flags.

### A3. Smoke Nivel 2 ejecutado una vez — ⏳ Pendiente (humano)

El doc `docs/smoke-fase1-20261011.md` está escrito y el runner
`scripts/smoke-nivel2.sh` lo automatiza (levanta el stack, firma y deposita el
artefacto como el CI, verifica inline + resumen + Teams). **Falta ejecutarlo** (o
se ejecuta cuando haya `twg` autenticado e infra desplejada, o contra un PR y
secretos reales).

- [ ] Elegir un PR real y ejecutar `scripts/smoke-nivel2.sh <pr_id>`.
- [ ] Verificar inline por hallazgo + resumen + un único teaser de Teams.
- [ ] Guardar la evidencia (IDs de comentario o captura) en `docs/`.

---

## B. Robustez del pipeline (P1)

### B1. Dedupe contra comentarios previos del agente (re-push / re-run) — ✅ Resuelto (2026-10-11)

**Era el hueco más importante del runtime.** El publish ahora lee lo ya publicado
(`vcs.list_review_markers`) antes de escribir.

**Solución:** cada inline lleva una marca `<!-- reviews:<hash>:inline:<finding_id> -->`
y el resumen `<!-- reviews:<hash>:ok|partial -->`. Antes de publicar, `publish` lee
los comentarios existentes y **salta** los hallazgos ya reportados y el resumen ya
completo. La lectura nunca aborta: si el VCS no responde, se publica igual (mejor un
duplicado que una review perdida). Tests con FakeVcs que ya trae marcadores previos.

- [x] Marcas reconocibles por hallazgo y por resumen (idempotencia).
- [x] Lectura previa de comentarios (`list_review_markers`) y salto de duplicados.
- [x] Test con FakeVcs que ya trae una review previa del agente.

> Nota crítica: el dedupe entre `standards` y `correctness` del nodo `merge` es
> **nominal**. Dos modelos casi nunca coinciden en el texto del título, así que en
> la práctica no deduplica nada de valor. El dedupe que importa es *este* (contra
> lo ya publicado), y ya está implementado.

### B2. Cola sin ack, sin reintentos y sin DLQ — ✅ Resuelto (2026-10-11)

`process_request` hacía `BLPOP` (el job **ya salió** de la cola) y ante cualquier
excepción solo registraba el log: el job **se perdía**, así que un fallo
transitorio de `twg` o de Teams pasaba de «un reintento» a «review que nunca
ocurrió».

**Solución:** `process_request` propaga la excepción y `_consume` decide con ella.
Ante un fallo transitorio reencola el mismo request con `attempts + 1` hasta
`MAX_REVIEW_ATTEMPTS` (3 por defecto); agotados, envía el job a la **DLQ**
(`reviews:jobs:dead`) con su `review_key`, intentos y motivo. Las reviews
**incompletas** no se reencolan: la ingesta diferida ya esperó al artefacto
(barrera/TTL) y re-ejecutar duplicaría la publicación, que ya corrió en el nodo
`publish`.

- [x] Reintento con contador (`ReviewRequest.attempts` + `MAX_REVIEW_ATTEMPTS`).
- [x] `review_key` explícito en el log al agotar los intentos.
- [x] **DLQ explícita:** `dead_letter(...)`/`dead_letters()` en `ReviewQueue`
      (Redis e in-memory). El job que agota intentos ya no se pierde.

### B3. Procesamiento secuencial (un solo worker) — ⏳ Pendiente (medición en vivo)

Un PR grande (PHPStan con decenas de hallazgos → hasta 20 `diff-line` + 20 inlines
+ resumen + Teams) bloquea la cola durante toda su duración: cada `diff-line` es un
`subprocess` a `twg`. No hay pool ni paralelismo.

- [ ] Medir el tiempo real en el smoke Nivel 2 (A3).
- [ ] Si supera unos segundos, paralelizar por `pr_id` (varios consumers) o acotar
      el número de `diff-line`.

### B4. `expected_tools` tiene dos fuentes que pueden divergir — ✅ Resuelto (2026-10-11)

La **barrera** de ingesta se construía desde `Settings.expected_tools` (env) mientras
el **perfil del repo** declaraba `expected_tools: [phpstan]`. Si divergían, la
barrera esperaba un conjunto distinto al que el perfil declaraba.

**Solución:** `reviews.profile` es la **fuente única de verdad**. El perfil manda
por defecto; el `.env` solo lo sobreescribe si se fija explícitamente un valor no
vacío (y se avisa de la divergencia). Test que fija la precedencia.

- [x] Una sola fuente de verdad (`load_profile` + `expected_tools`).
- [x] Test de precedencia y de override explícito.

### B5. Scope sin filtro de ruido ni tope de ficheros — ✅ Resuelto (2026-10-11)

`max_files_per_pr: 10` estaba declarado en `autonomy.yaml` y **no se aplicaba**;
`get_diffstat` estaba implementado y **nunca se usaba**.

**Solución:** el nodo `scope` filtra ficheros ruidosos antes de las personas
(lockfiles, `var/**`, `vendor/**`, generados; ampliable con `SCOPE_IGNORE`) y, si
`max_files_per_pr` está declarado, recorta el diff a los ficheros con más churn
usando `get_diffstat`. Así un `composer.lock` enorme no gasta tokens ni inlines.

- [x] Filtro de ruido (`DEFAULT_IGNORE_PATTERNS` + `filter_diff`).
- [x] `max_files_per_pr` aplicado con `limit_files`/`get_diffstat`.
- [x] Tests de filtrado y de truncado por churn.

### B6. Publicación no atómica — ✅ Decidido (2026-10-11)

Si `post_comment` (resumen) falla **después** de publicar los inlines, quedan
inlines sin resumen. **Decisión** en `docs/decisiones-publish-inline-20261011.md`:
el resumen se publica **al final**, como cierre; si falla, los inlines ya anclados
se conservan (no se compensa) y el fallo se registra para reintento. La marca de
dedupe se escribe **por hallazgo** (inline) y **por commit** (resumen), así que un
reintento no duplica lo ya publicado.

- [x] Orden definido (resumen último) y comportamiento ante fallo documentado.

### B7. Sin watchdog ni healthchecks de servicio — ✅ Resuelto (2026-10-11)

**Solución:** `api`, `worker`, `postgres` y `redis` arrancan con
`restart: unless-stopped`. Hay healthcheck para la API (`/healthz`), Postgres
(`pg_isready`) y Redis (`redis-cli ping`). El worker escribe un latido en
`/tmp/reviews-worker-heartbeat`; el healthcheck comprueba su mtime, así que un
worker colgado se reinicia solo.

- [x] `restart: unless-stopped` en api/worker/postgres/redis.
- [x] Healthcheck del worker basado en latido (y de api/postgres/redis).

---

## C. Calidad de la señal (P2)

### C1. Revisión humana del formato inline (checkpoint B abierto) — ⏳ Pendiente (humano)

La casilla «Formato inline revisado por humano» sigue desmarcada. El inline es el
entregable visible: si el texto no convence, la Fase 1 falla de producto aunque
todos los tests pasen. La plantilla actual (`_render_inline`/`_render_summary`) y la
propuesta de formato están congeladas como especimen en
`docs/decisiones-publish-inline-20261011.md` para que la revisión sea sobre algo
concreto.

- [ ] Revisar un hilo inline real (del smoke) y ajustar plantilla y resumen.
- [ ] Decidir mención a autor, idioma (ES/EN) y longitud máxima.

### C2. Un único tipo de anclaje; borrados puros sin inline — ✅ Decidido (2026-10-11)

`resolve_inline_anchor` ancla con la primera evidencia; si falla, el hallazgo cae al
resumen. **Decisión** (`docs/decisiones-publish-inline-20261011.md`): los hallazgos
en líneas **eliminadas** (sin lado `+`) caen al resumen, no se fuerza un anclaje
artificial; se documenta como guardrail y se revisará si aparece volumen.

- [x] Comportamiento definido para hallazgos en líneas borradas.

### C3. LLM sin medición de falsos positivos ni corte de presupuesto — ✅ Corte resuelto (2026-10-11)

`budget_tokens_per_pr` solo se logueaba. **Solución:** `LLMReviewer` estima los
tokens del prompt (~4 chars/token) y **corta** la llamada (devuelve `None`) si
prompt + `max_tokens` supera el presupuesto, exponiendo `last_estimated_tokens`.

- [x] Corte real del presupuesto, no solo log.
- [ ] **Medición de aciertos** (ratio de hallazgos aceptados/resueltos): sigue
      abierta — requiere datos de uso real, no aplica aún en report-only.

### C4. Tema protegido inferido por heurística débil — ✅ Ampliada (2026-10-11)

Compartido con la estática: la lista de pistas (`auth`, `password`, `sql`…) era
mínima. **Solución:** `_PROTECTED_HINTS` se amplió por dominio (autenticación,
secretos, inyección SQL/código, memoria, concurrencia, pérdida de datos,
criptografía, ruptura de contrato público) y se afinó para no marcar de más.

- [x] Heurística ampliada y con tests.
- [ ] **Revisión con seguridad** antes de Fase 3 (decide el autofix): pendiente de
      equipo externo.

---

## D. Configuración, contrato y decisiones abiertas (P2/P3)

### D1. Clamp `report` en YAML vs override por deploy — ✅ Resuelto (2026-10-11)

**Decisión** (decisión abierta #4 del plan): el clamp vive en
`config/autonomy.yaml` (visible, versionado) y un despliegue puede fijar
`MAX_AUTONOMY_OVERRIDE` en settings, pero **solo puede bajar el techo**, nunca
subirlo por encima de lo que el YAML declara. `_lower_cap` lo garantiza.

- [x] Override por settings que solo puede bajar el techo (`max_autonomy_override`).
- [x] Test de que no puede elevar por encima del YAML.

### D2. Plantilla del artefacto acoplada por convención — ✅ Resuelto (2026-10-11)

`tooling_output_template` vivía en `Settings` sin validación. **Solución:** se
valida en el arranque de la API y del worker (`validate_tooling_template`) que la
ruta resuelve; un cambio en el CI rompe ruidoso al arrancar, no al leer.

- [x] Validación en el arranque de la ruta del artefacto.

### D3. Sin límite de tamaño en el `POST` de tooling — ✅ Resuelto (2026-10-11)

**Solución:** `Settings.max_tooling_payload_bytes` y validación en
`/webhook/tooling` que responde **413** (`HTTP_413_CONTENT_TOO_LARGE`) si el payload
lo supera.

- [x] Límite configurable + respuesta 413.

### D4. Sin rate-limit ni autenticación de origen en `/webhook/tooling` — ✅ Resuelto (2026-10-11)

**Solución:** `require_production_secrets` hace **fallar el arranque** si
`environment=production` y algún secreto sigue en `change-me`, así la firma HMAC no
se omite en producción. (El rate-limit sigue sin implementar; la firma + tamaño
acotan el riesgo.)

- [x] Fallar el arranque en producción si un secreto sigue en `change-me`.
- [ ] **Rate-limit** explícito: pendiente, no bloqueante (lo cubre el ingress).

---

## E. Deuda e higiene (P3) — ✅ Resuelta salvo reservas explícitas (2026-10-11)

- [x] **`get_diffstat` sin uso** y **`max_files_per_pr` sin aplicar** → resueltos
      en B5.
- [x] **Sin CI propio.** Añadidos `.github/workflows/ci.yml` (ruff + mypy + pytest
      en push/PR) y `Makefile` (`make install` / `make ci`).
- [x] **`mypy` con errores preexistentes.** Añadido `types-pyyaml` y anotaciones;
      `mypy` strict limpio.
- [x] **Tests de `TeamsWebhookAdapter`** con mock HTTP (fallo, timeout, éxito).
- [ ] **Postgres/checkpointer cableado y reservado.** Se mantiene como
      checkpointer durable de Fase 2; **no** se retira del compose. Documentado
      como reservado.
- [ ] **Observabilidad mínima** (trazas LangSmith, métricas de coste/FP,
      correlación por `review_key`): sigue pendiente, no bloquea report-only.

---

## Orden recomendado para cerrar

Lo único que queda es humano/externo; el código de esta ronda está cerrado.

1. **A2** (arranque reproducible) → ✅ hecho. **A3** (smoke con `twg` real) → ⏳
   ejecutarlo una vez. A1 pasa a opcional: sin él se pierde la anotación de un
   pipeline rojo, no la Fase 1.
2. **B1** (dedupe re-push) → ✅ hecho. Era lo que mataba la confianza en el bot.
3. **B7 + B2** (que no se muera y que no pierda jobs en silencio) → ✅ hecho.
4. **C1** (formato humano) → ⏳ revisar el espécimen de
   `docs/decisiones-publish-inline-20261011.md`.
5. **B4/B5/D1–D3** (fuentes de verdad y límites) → ✅ hecho.
6. **E** (CI verde propio) → ✅ hecho.

## Definición de hecho de la Fase 1 (reafirmada)

- Hallazgos **anclados inline** en el diff + resumen en el PR + teaser en Teams.
- **Cero** escritura de código en el repo del PR (sin push/commit/branch).
- Ninguna `Decision.action` es `fix_*`.
- Una review real, disparada por el `POST` del CI (o por el puente equivalente),
  publicada y **revisada por una persona** que diga «esto lo leería».

Con A2 en verde (✅), queda **A3** (ejecutar el smoke) y **C1** (validación humana del
formato). Con ambas en verde se puede declarar la Fase 1 hecha y pasar a decidir la
Fase 2 (HITL, Opción B). A1 suma, pero no bloquea: ver
`docs/ingesta-vs-ejecucion-20261011.md`.
