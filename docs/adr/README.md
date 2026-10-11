Última modificación: 2026-10-11 | Creado: 2026-10-11

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

Las especificaciones de fase ([`diseno-agente-code-review`](../iniciativas/001-diseno-base/spec.md),
[`fase2-hitl`](../iniciativas/002-fase2-hitl/spec.md), [`fase3-fix-verificacion`](../iniciativas/003-fase3-fix-verificacion/spec.md))
conservan el razonamiento completo. Estos ADR son el resumen normativo.
