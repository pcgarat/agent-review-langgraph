Última modificación: 2026-10-16 | Creado: 2026-10-11

# Contexto y lenguaje del dominio

Vocabulario compartido de Agent Review LangGraph. Cuando el código, un ADR o una
conversación usan una de estas palabras, significan **esto** y nada más. Si un
término nuevo aparece en el código o en una decisión, entra aquí en el mismo cambio.

Las decisiones que dan forma a estos conceptos viven en [`docs/adr/`](docs/adr/); la
arquitectura, en [`docs/arquitectura.md`](docs/arquitectura.md).

---

## Qué es el sistema, en una frase

Un agente que revisa los PRs de **un** repositorio: espera a que el CI le entregue la
salida de sus herramientas, produce **hallazgos**, decide por cada uno qué hacer según
una **política** versionada, puede **pausarse** para preguntar a una persona, puede
proponer y **verificar** un arreglo en un sandbox, y publica el resultado en el PR y en
Teams. **Nunca escribe en el repositorio revisado.**

---

## Identidad de una review

| Término | Definición | En el código |
|---|---|---|
| **Review** | El trabajo completo sobre un PR en un commit concreto: leer el diff, producir hallazgos, decidir, publicar. | `ReviewRequest` (entrada), `ReviewOutcome` (salida) |
| **Clave de commit** | `repo:pr:head_sha`. Identifica *qué* se revisa. | `ReviewRequest.idempotency_key` |
| **Ejecución** | Una review disparada por un pipeline concreto del CI. Dos pipelines sobre el mismo commit son **dos ejecuciones** y publican dos reviews. | `ReviewRequest.review_key` = `repo:pr:sha:pipeline_id` |
| **Clave de ingesta** | `repo:pr:sha:tool`. Identifica un evento de CI que entrega un artefacto. | `ReviewRequest.ingest_key(tool)` |
| **Hilo** | Estado durable de una ejecución en el checkpointer. Es la `review_key`, salvo la pasada degradada, que añade `:partial`. | `ReviewRequest.thread_id` |
| **Token de dedupe** | `reviews:<sha256(review_key)[:12]>`. Firma lo que una ejecución publica. | `ReviewRequest.dedupe_token` |
| **Marca** | Comentario HTML con el token dentro de lo publicado. **Marca de resumen** `<!-- token:ok -->` o `:partial`; **marca inline** `<!-- token:i:<8 base32> -->` por hallazgo. Se leen del PR para no publicar dos veces. | `_summary_token`, `_inline_token` en `graph/build.py` |

> **Regla:** ejecución, hilo y token de dedupe tienen la **misma** identidad (salvo el
> sufijo `:partial` del hilo). Ver [ADR-0005](docs/adr/0005-identidad-de-la-ejecucion.md).

---

## Entrada: ingesta diferida

| Término | Definición |
|---|---|
| **Artefacto** | Salida cruda de una herramienta estática (hoy PHPStan en JSON) que el CI del repo revisado manda a `POST /webhook/tooling`. Se guarda en `var/tooling/<pr>/<sha>/<tool>.json`. |
| **Ingesta diferida** | La review se encola cuando ya han llegado los artefactos que necesita, no cuando se abre el PR. |
| **Herramientas esperadas** | Artefactos que la ingesta espera antes de encolar (`expected_tools` del perfil; hoy `[phpstan]`). |
| **Barrera** | La condición «han llegado todas las herramientas esperadas». |
| **`PendingReview`** | Registro en Redis de una review que todavía espera artefactos. Estados: `pending` → `ready` o `degraded`. |
| **TTL de ingesta** | `ttl_ready` (20 min, cuando ya llegó algo) y `ttl_pending` (24 h, cuando no llegó nada). Al vencer, la review se degrada. |
| **Pasada degradada** | Ejecución que se lanza al vencer el TTL sin todos los artefactos. Tiene su propio hilo (`:partial`) y publica con aviso de review incompleta. |
| **Puente desechable** | Cómo se ve la ingesta por artefacto: un ahorro de coste en Fase 1, no la base del sistema. Ver [ADR-0004](docs/adr/0004-ingesta-diferida-por-artefacto-del-ci.md). |

---

## Hallazgos y personas

| Término | Definición |
|---|---|
| **Hallazgo** (`Finding`) | Un problema concreto en una línea de un fichero, con el contrato de `ce-code-review`: `severity`, `autofix_class`, `owner`, `confidence`, `evidence`, `why_it_matters`, `suggested_fix`, `protected_subject`… |
| **Severidad** | `P0` (corrupción, caída, brecha) … `P3` (estilo). Rúbrica en [`contracts/action-class-rubric.md`](contracts/action-class-rubric.md). |
| **Confianza** | Anclada a `0/25/50/75/100`. Cualquier otro valor es inválido. |
| **`autofix_class`** | `gated_auto` (arreglable con control), `manual` (decisión de diseño), `advisory` (solo informar). |
| **Tema protegido** (`protected_subject`) | Auth, cripto, concurrencia, pérdida de datos, inyección, secretos, memoria, contrato público. Nunca se arregla solo. |
| **Persona** | Un revisor que produce hallazgos sobre el diff. Corren en paralelo (fan-out). |
| **`standards`** | Persona estática (`ToolingReviewer`): convierte el artefacto de PHPStan en hallazgos anclados al diff. No ejecuta la herramienta. |
| **`correctness`** | Persona LLM (`LlmReviewer`), opt-in. Sus hallazgos se validan contra el contrato; una línea inventada se descarta. |
| **`null`** | Persona nula (`NullReviewer`): se usa si no hay ninguna otra. |
| **Rol** | La unidad con la que se asigna modelo: `correctness`, `security`, `arch` (personas), `fixer`. Cada rol se bindea a un modelo en `config/models.yaml`. No es lo mismo que un nodo del grafo: `persona` es un fan-out y todas sus ramas comparten closure. |
| **Registro de modelos** | `config/models.yaml` ([ADR-0015](docs/adr/0015-registro-declarativo-de-modelos.md)): proveedores, modelos con sus capacidades y el binding de cada rol. Se lee al arrancar. |
| **Binding** | La asignación rol → modelo dentro del registro. |
| **Capacidad** | Lo que un modelo declara saber hacer (`json_mode`, contexto). Se declara en el registro porque `/v1/models` no lo publica; el adaptador pide al modelo solo lo que declara. |
| **Estado de la fuente** | `present` / `missing` / `unreadable`. Separa «análisis limpio» de «no se pudo leer la salida». |
| **Review incompleta** | Alguna fuente estática declarada no estaba `present`. Su «0 hallazgos» **no** significa «PR limpio» y se publica con aviso. |
| **Merge** | Paso que deduplica: mismo `(fichero, línea, título)`, o mismo `(fichero, título)` a ≤5 líneas visto por **personas distintas**. Conserva la evidencia más rica. |
| **Scope** | Recorte del diff antes de las personas: fuera el ruido (lockfiles, `vendor/**`, `var/**`, generados) y, si se declara, solo los `max_files_per_pr` ficheros con más churn. |

---

## Política y autonomía

| Término | Definición |
|---|---|
| **Política** | `config/autonomy.yaml`: reglas ordenadas (gana la primera que casa), guardas y techo. Determinista, sin LLM. |
| **Acción** (`FindingAction`) | Lo que la política asigna a un hallazgo: `report`, `escalate`, `fix_with_approval`, `fix_auto`. |
| **Acción pendiente** | `escalate` o `fix_with_approval`: necesita a una persona y **pausa** la review. |
| **Decisión de política** (`Decision`) | Resultado de la política para un hallazgo: acción, razón, `matched_rule` y `policy_version`. **No** confundir con la decisión humana. |
| **Techo** (`max_autonomy`) | Máxima autonomía permitida. Es un parámetro de uso: lo ajusta quien opera el agente. Con `report` (el valor por defecto), todo (también `escalate`) se degrada a `report`. |
| **Clamp** | Aplicar el techo a la acción de una regla. |
| **Override del techo** | `MAX_AUTONOMY_OVERRIDE`: un despliegue puede **bajar** el techo del YAML, nunca subirlo. |
| **Guarda** | Límite de `guards:` en el YAML (`max_inline_comments`, `max_files_per_pr`, `max_fix_attempts`…). |
| **Ruta nunca-auto** | Fichero que casa con `never_auto_paths` (migraciones, `docker/**`, `.github/**`, auth…). Convierte la acción en `fix_with_approval` salvo que las reglas digan `escalate`, y bloquea cualquier fix. |

---

## HITL: pausa y decisión humana

| Término | Definición |
|---|---|
| **Pausa** | `interrupt` de LangGraph en el nodo `hitl` cuando hay acciones pendientes. Es durable: el estado vive en Postgres y sobrevive a un reinicio. |
| **Solicitud de aprobación** (`ApprovalRequest`) | Carga de la pausa: los `PendingDecision` (hallazgo + acción propuesta + razón) y la orden de CLI para decidir. |
| **`PendingDecision`** | Un hallazgo pendiente con lo necesario para decidirlo sin abrir el PR: título, severidad, `fichero:línea`, la explicación del problema y el enlace al código. |
| **Bot de Teams** (`teams-bot`) | Proceso que consume la outbox, publica/edita en el canal y recibe las acciones de la tarjeta (`POST /api/messages`). Traduce a `HitlDecision` con `build_decision` y **encola**; no reanuda el grafo ni conoce el worker. |
| **Hilo por PR** | Un mensaje raíz por `repo:pr` en el canal dedicado; el resto de avisos van como reply y la tarjeta se edita in-place. El mapeo vive en Redis (`reviews:teams:threads`) y es UI, no verdad: si se pierde, se abre otro hilo. |
| **Tarjeta de decisión** | La Adaptive Card que el bot publica en Teams a partir de la `ApprovalRequest`. Un botón por hallazgo y un Enviar gobernado por el **estado por usuario** (`reviews:teams:card:<key>`, lo que cada uno ha marcado). |
| **Inbox** | Índice en Redis de las reviews pausadas (`reviews:paused:<review_key>`). Sirve para descubrirlas; la verdad está en el checkpoint. |
| **Aviso de pausa** | Aviso al canal (Teams) de que una review quedó pausada. Viaja por la **outbox** con la `ApprovalRequest` estructurada; **no lleva marca de dedupe.** |
| **Outbox del canal** | Cola Redis `reviews:notices` donde el worker **encola** los avisos (`ChatNotice`) en vez de llamar por HTTP; el bot los consume con `BLPOP`. Un bot caído no pierde el aviso: se publica al arrancar |
| **`ChatNotice`** | Aviso del worker al canal: `kind` `published` (teaser de una review) o `pause` (tarjeta de decisión, con la `ApprovalRequest`). Lleva siempre `repo`/`pr_id`/`review_key` para resolver el hilo |
| **Decisión humana** (`HitlDecision`) | Respuesta de una persona: `actor`, `decision_id` e `items`, con un resultado por cada hallazgo pendiente. Debe cubrir **todos** los pendientes. |
| **Resultado** (`DecisionOutcome`) | `approved` (autoriza el arreglo), `rejected` (descarta el hallazgo), `noted` (se atiende fuera del agente), `expired` (lo emite el sistema al vencer el TTL). |
| **Reanudación** | Job con `ReviewRequest.decision`: el worker hace `Command(resume=…)` sobre el **mismo** hilo. |
| **Expiración** | Pasado `DECISION_TTL_S` (7 días), el barrido reanuda con `expired` y `actor=system:ttl`. Una review nunca se queda colgada. |
| **Reconciliación** | El worker vuelve a meter en el inbox los hilos pausados que el checkpointer conoce y el inbox ha perdido. |
| **Poda** | Borrado de hilos **completados** más antiguos que `CHECKPOINT_RETENTION_DAYS`. Un hilo pausado **humano** nunca se poda; uno pausado en `fix_wait` sí, con `FIX_WAIT_MAX_AGE_S` (`FixWaitRetention`). |

---

## Fix y verificación (Fase 3)

| Término | Definición |
|---|---|
| **Candidato a fix** | Hallazgo con acción `fix_*` **y** resultado `approved`. Aprobar un `escalate` no produce fix. |
| **Parche propuesto** (`ProposedPatch`) | Diff unificado de un fichero y un hunk, más el rango `start_line..end_line` y el texto de reemplazo. |
| **Fixer** | Quien propone el parche (`LlmFixer`). Solo propone; no ejecuta ni escribe. |
| **Validación local** | Comprobación pura antes del sandbox: el fichero del hallazgo, un hunk, ≤ `max_fix_lines` y un rango dentro de las líneas **añadidas** del PR. |
| **Verificación** | Aplicar el parche en el sandbox y pasar las recetas del perfil con la imagen del CI fijada por digest: primero los tests dirigidos del fichero tocado y, al final, la suite completa. |
| **Receta** | Comando con nombre declarado en `checks:` del perfil (`typecheck`, `arch`, `lint`, `targeted`, `tests`). El sandbox solo ejecuta recetas declaradas, nunca shell libre. |
| **Perfil del repo** | `config/repo_profile.*.yaml`: identidad, imagen del CI y su digest, `setup`, `checks`, orden de `verify` y `expected_tools`. |
| **Sandbox runner** | Servicio aislado que clona, aplica el parche, ejecuta y destruye. `POST /verify`, idempotente por `run_key`. |
| **`run_key`** | `review_key:finding_id:intento`. Una reentrega con el mismo `run_key` devuelve el informe previo y no vuelve a ejecutar. |
| **Estado del fix** (`FixStatus`) | `verified`, `failed` (agotó intentos), `invalid_patch`, `not_verifiable` (sin digest, sin runner o sin fixer), `skipped` (tope), `blocked` (ruta nunca-auto o tema protegido). |
| **Entrega** | Cómo aparece un fix verificado en el inline: un bloque ` ```diff ` (por defecto) o ` ```suggestion `. La elige un `DeliveryFormatter`. |
| **Handoff del fix (E7)** | La verificación no vive en el worker de reviews: `fix_dispatch` propone y valida, `fix_wait` **pausa** con la *wave* y el `fix-worker` la verifica y reanuda. Desacople por cola (`reviews:fixes`), no por llamada. |
| **`FixWaitRequest` (wave)** | `interrupt` de la pausa de fix (`kind="fix"`): el `review_key` y los `VerifyJob` (`run_key`, fichero, parche, recetas) que el `fix-worker` debe verificar. |
| **`PENDING`** | Estado del fix con parche válido y verificación despachada: el inline lo rotula "en verificación" y no se entrega sugerencia hasta que llegue la resolución. |
| **`fix-worker`** | Servicio que consume `reviews:fixes`, llama al `sandbox-runner` y encola la reanudación con la `FixResolutions`. Nunca publica ni invoca el grafo. |
| **`FixResolutions`** | Resultado de una wave: una `FixResolution` por `run_key` (`passed`/`failed`/`error`), con los checks y el `image_digest`. Reanuda el hilo. |
| **Dedupe del fix** | El `run_key` se marca en `reviews:fixes:done`: una reentrega no relanza la verificación; el worker solo aplica una resolución cuyo `run_key` esperaba. |
| **TTL del handoff** | `FIX_VERIFY_TTL_S`: una wave sin resolver vence y `sweep_fix_expired` reanuda con `outcome="error"` → el fix queda `NOT_VERIFIABLE`, nunca colgado. |

---

## Fiabilidad

| Término | Definición |
|---|---|
| **Fallo transitorio** | Puede resolverse reintentando: red, timeout, 5xx, 429, `TwgError`, `LlmTransientError`. Lo decide `errors.is_transient`. |
| **Fallo permanente** | Reintentar lo repite: 4xx, respuesta malformada, bug. |
| **Reintento de nodo** | `RetryPolicy` de LangGraph sobre los nodos con efectos externos. Reintenta en el sitio, sin sacar el job de la cola. |
| **Reintento de job** | El worker reencola la review entera (`attempts+1`); al agotar `MAX_REVIEW_ATTEMPTS` la manda a la DLQ. |
| **DLQ** | `reviews:jobs:dead`: jobs descartados, con su `review_key`, intentos y motivo. |
| **Continuar desde el checkpoint** | Un hilo a medias se reanuda con `invoke(None)` desde el nodo que falló, nunca con una pasada nueva desde `START`. |

---

## Evaluación de la review (gold set)

| Término | Definición |
|---|---|
| ***Gold set*** | El conjunto versionado de casos auditados a mano (`tests/eval/pr_3/`), con diff, `scope`, etiquetas y grabaciones. La fuente única del baseline es la spec [006](docs/iniciativas/006-eval-de-la-review/spec.md). |
| **Caso de evaluación** | Un directorio del *gold set*: `labels.yaml` (procedencia, `scope`, etiquetas), `diff.patch`, `recorded.json` y `expected_report.json`. |
| **Etiqueta (`expected`/`must_not_report`/`acceptable`)** | Veredicto humano por hallazgo: debe salir, no debe salir (con motivo) o es cierto pero sin valor. Un hallazgo no etiquetado cuenta como falso positivo. |
| ***Replay*** | Correr el grafo real con un `LlmPort` que devuelve las respuestas grabadas, abortando si el *fingerprint* del prompt o del caso no casa. Es el modo de CI. |
| ***Fingerprint*** | Huella del prompt (rol + `system` + prompt + `max_tokens`) y del caso (nombre + diff + `scope`). Una grabación vieja con un prompt nuevo aborta en vez de medir el pasado ([ADR-0017](docs/adr/0017-replay-determinista-con-fingerprint.md)). |
| **Mejora (dominancia)** | Relación entre dos corridas: precisión, recall y cobertura no bajan, los falsos `P0`/`P1` no suben y algo sube estrictamente. Lo demás es *no concluyente*. |
| **Cobertura** | Ficheros de código y configuración que el `LlmReviewer` vio / los de esas clases en el PR. Sale de `last_seen_files`. |
| **`tp_inverted`** | `expected` cuyo fallo se describió al revés (`must_mention` no cumplido). Sigue siendo `tp`: no mueve precisión ni recall. |

---

## Ambigüedades resueltas

- **«Decisión»** tiene dos sentidos. En el código, `Decision` es **siempre** la de la
  política y `HitlDecision`/`FindingDecision` la humana. En prosa, di «decisión de
  política» o «decisión humana».
- **«Aprobar»** solo autoriza a intentar un arreglo dentro de lo permitido. No salta
  rutas nunca-auto ni temas protegidos ([ADR-0009](docs/adr/0009-la-proteccion-manda-sobre-la-aprobacion.md)).
- **«Review publicada»** significa que esta ejecución dejó su marca de resumen. Un
  aviso de pausa no cuenta como publicación.
- **«Sin hallazgos»** solo significa «PR limpio» si la review está completa.
- **«Teams»** es Microsoft Teams (canal de avisos y decisión vía bot). El Incoming
  Webhook de Office 365 Connectors está **retirado**; no hay `TEAMS_WEBHOOK_URL`.
  `twg teams` es Atlassian Teams y no tiene nada que ver.
- **«Canal»** en este dominio es el medio de avisos y decisión humana (hoy Teams).
  No confundir con un canal de Redis ni con el *messaging endpoint* HTTP.
- **El CLI se llama `agent_review`** (`[project.scripts]`). Las specs de Fase 2 y 3
  dicen `reviews`, que era el nombre anterior.
