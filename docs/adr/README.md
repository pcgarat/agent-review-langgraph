Última modificación: 2026-10-24 | Creado: 2026-10-11

# Registro de decisiones (ADR)

Una decisión por fichero: contexto, decisión, consecuencias y alternativas
descartadas. Un ADR no se reescribe cuando cambia la decisión: se marca como
**Sustituido por ADR-NNNN** y se añade uno nuevo. El vocabulario está en
[`CONTEXT.md`](../../CONTEXT.md).

| # | Decisión | Estado |
|---|---|---|
| [0001](0001-langgraph-con-checkpointer-postgres.md) | LangGraph como motor, con checkpointer durable en Postgres | Aceptado |
| [0002](0002-contrato-de-ce-code-review-sin-su-runtime.md) | Copiar el contrato de hallazgos de `ce-code-review`, no invocar su runtime | Aceptado |
| [0003](0003-autonomia-como-politica-por-hallazgo.md) | La autonomía es una política por hallazgo con un techo global | Aceptado |
| [0004](0004-ingesta-diferida-por-artefacto-del-ci.md) | La review se dispara por el artefacto del CI, no por el PR | Aceptado |
| [0005](0005-identidad-de-la-ejecucion.md) | Ejecución, hilo y marca de dedupe comparten identidad | Aceptado |
| [0006](0006-publicacion-inline-primero-resumen-al-final.md) | Inline primero, resumen con marca al final, sin compensación | Aceptado |
| [0007](0007-reintento-en-dos-capas.md) | Reintento en dos capas con clasificación propia de errores | Aceptado |
| [0008](0008-hitl-por-cli-y-http-con-cola-unica.md) | HITL por CLI/HTTP, cola única y reanudación en el worker | Aceptado |
| [0009](0009-la-proteccion-manda-sobre-la-aprobacion.md) | La protección de seguridad manda sobre la aprobación humana | Aceptado (sustituye a D3 de la spec de Fase 2) |
| [0010](0010-fix-entregado-como-sugerencia.md) | El fix se entrega como sugerencia; el agente no escribe en el repo | Aceptado |
| [0011](0011-sandbox-runner-aislado.md) | Verificación en un `sandbox-runner` aislado, rootless y con la imagen por digest | Aceptado |
| [0012](0012-vcs-tras-un-puerto.md) | El VCS vive tras un puerto; `twg` con cuenta de servicio y GitHub como alternativa | Aceptado |
| [0013](0013-secretos-por-entorno-fail-closed.md) | Secretos solo por entorno y arranque *fail-closed* en producción | Aceptado |
| [0014](0014-criterio-de-merge-entre-personas.md) | Criterio único de dedupe y fusión entre personas | Aceptado |
| [0015](0015-registro-declarativo-de-modelos.md) | Registro declarativo de modelos por rol, con el `LlmPort` negociando capacidades | Aceptado |
| [0016](0016-bot-de-teams-para-el-canal-de-decision.md) | Un bot de Teams es el canal de decisión, con un hilo por PR | Aceptado |
| [0017](0017-replay-determinista-con-fingerprint.md) | Un *gold set* versionado se mide en CI con respuestas grabadas y *fingerprint* | Aceptado |
| [0018](0018-workspace-de-lectura-en-el-runner.md) | El `sandbox-runner` también sirve lecturas del repo desde un clon *bare* | Aceptado |
| [0019](0019-presupuesto-de-tokens-por-review.md) | El presupuesto de tokens es un contador compartido por review, no un entero por llamada | Aceptado |
| [0020](0020-panel-como-servicio-aparte.md) | El panel es un servicio aparte: *read model* + comandos encolados | Aceptado |
| [0021](0021-cascada-de-verificacion-del-hallazgo.md) | La verificación del hallazgo es una cascada barata→cara cuyo estado viaja en el contrato | Aceptado |
| [0022](0022-contrato-de-secretos-declarado.md) | El contrato de secretos se declara en el modelo, no en listas a mano | Aceptado |
| [0023](0023-razonamiento-por-streaming-y-stream-de-redis.md) | El razonamiento de los revisores se captura por *streaming* y viaja por un *stream* de Redis por review | Aceptado |
| [0024](0024-gate-de-severidad-en-la-politica.md) | La severidad publicada la decide un gate de política, no el modelo | Aceptado |
| [0025](0025-matcher-del-gold-set-mide-el-defecto.md) | El matcher del *gold set* mide el defecto (localización + fragmento discriminante), no la redacción | Aceptado |
| [0026](0026-contrato-de-lectura-con-tamano-real.md) | La lectura devuelve un value object con el tamaño real, no un `str` | Aceptado |
| [0027](0027-dialecto-portable-de-grep.md) | `grep` habla un dialecto portable y rechaza lo que cada motor lee distinto | Aceptado |
| [0028](0028-cli-claude-fuera-del-worker.md) | El CLI de Claude corre fuera del worker; el núcleo solo ve hallazgos ya validados | Aceptado |
| [0029](0029-lanzamiento-desde-el-panel.md) | Una review se lanza desde el panel con la URL del pull request; el worker lee el commit | Aceptado |
| [0030](0030-bandeja-web-de-la-decision.md) | La bandeja del panel es la otra superficie de la misma pregunta; vale la primera respuesta | Aceptado |
| [0031](0031-configuracion-desde-el-panel.md) | Los diales de una review se publican desde el panel; el hilo fija la foto al empezar | Aceptado |

Las especificaciones de fase ([`diseno-agente-code-review`](../iniciativas/archivo/001-diseno-base/spec.md),
[`fase2-hitl`](../iniciativas/archivo/002-fase2-hitl/spec.md), [`fase3-fix-verificacion`](../iniciativas/archivo/003-fase3-fix-verificacion/spec.md))
conservan el razonamiento completo. Estos ADR son el resumen normativo.
