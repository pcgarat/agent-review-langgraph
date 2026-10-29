Última modificación: 2026-10-29 | Creado: 2026-10-11


# Iniciativas

Cada iniciativa tiene su carpeta `NNN-slug/` con `spec.md` (qué y por qué),
`plan.md` (cómo y en qué orden) y `todo.md` (qué falta). El ciclo de vida y la
nomenclatura están en la regla [`documentacion`](../../.agents/rules/documentacion.md).
Las cerradas sin pendientes se mueven a `archivo/`.

## Activas

| # | Iniciativa | Spec | Estado | Depende de / lo que queda |
|---|---|---|---|---|
| 010 | [Personas deterministas de wiring y configuración](010-personas-de-wiring/spec.md) | borrador | 🔵 Vía de recall | Fase 1 (gate de CI) sin dependencias; fase 2 (persona), 007 · 5 preguntas abiertas |
| 014 | [Revisor con herramientas de lectura](014-revisor-con-herramientas/spec.md) | aprobada | 🔵 Las personas LLM leen ficheros y hacen `grep` mientras revisan · [plan](014-revisor-con-herramientas/plan.md) · [todo](014-revisor-con-herramientas/todo.md) | 008 (012, 016 y 017 hechas) · RH1–RH10 hechas; RH11–RH12 pendientes |
| 015 | [Puesta en marcha real](015-puesta-en-marcha-real/spec.md) | borrador | 🟠 Smokes, pendientes humanos y endurecimiento de `production` | Sin dependencias de código · reúne los pendientes de 001–005 y 013 · **P7 primero** |
| 018 | [Trazas de la review](018-trazas-de-la-review/spec.md) | borrador | 🟠 Recorrido visible de una petición (D14); Langfuse v3 self-hosted | Sin dependencias · 5 preguntas abiertas · comparte la lectura del `usage` con 017 |
| 023 | [Eficacia del bucle de lectura](023-eficacia-del-bucle-de-lectura/spec.md) | aprobada | 🟠 Que `grep`, `read_file` y `list_dir` de 014 sirvan de verdad: dialecto portable único, contrato de lectura con metadatos, `forwarding` a las cuatro superficies, corte con tamaño real, `outline`, estado y `Reading` por review · [plan](023-eficacia-del-bucle-de-lectura/plan.md) · [todo](023-eficacia-del-bucle-de-lectura/todo.md) | 014 (medida) · 024 (S y colocación del bloque) · K1 y K2 cerrados (G1, R1, L1, P1, S1 y C1 hechas); K0 pendiente de T2/C0c (regrabación *live*) y K3 de M1 (informe por configuración) |
| 024 | [Contexto conversacional del bucle](024-contexto-conversacional-del-bucle/spec.md) | aprobada | 🟠 El bucle pasa a mensajes y se mide `cached_tokens` antes de decidir si la review se parte en fases (`plan`/`read`/`review`) · [plan](024-contexto-conversacional-del-bucle/plan.md) · [todo](024-contexto-conversacional-del-bucle/todo.md) | 023 (K0 y K3) · S1–S5 (medida) van antes de las corridas *live* de 023 · A tras K3 · B solo con número · S–D pendientes |
| 027 | [Configuración desde el panel](027-configuracion-desde-el-panel/spec.md) | aprobada | 🟠 El operador publica diales; cada control restaura su valor inicial; la review fija esa foto al empezar · [plan](027-configuracion-desde-el-panel/plan.md) · [todo](027-configuracion-desde-el-panel/todo.md) | 019 · C1–D1 hechas · [ADR-0031](../adr/0031-configuracion-desde-el-panel.md) |
| 031 | [Progreso en vivo del panel](031-progreso-en-vivo-del-panel/spec.md) | borrador | 🟠 Panel que se refresca solo, diagrama de pasos en la ficha, panel en vivo maximizable y tiempo en «En vuelo» | 029 · 030 · 3 preguntas abiertas |
| 032 | [La skill con el modelo del clúster](032-skill-con-modelo-de-la-cluster/spec.md) | aprobada | 🟠 el panel elige `claude`, `deepseek-v4-flash` o `glm5.3-flash`; falta el 429 y cerrar el egress del gateway · [plan](032-skill-con-modelo-de-la-cluster/plan.md) · [todo](032-skill-con-modelo-de-la-cluster/todo.md) · [ADR-0037](../adr/0037-la-skill-con-un-backend-ajeno.md) | 028 · el *gold set* no entra |

## Pendientes humanos o externos

Viven en un solo sitio: [015 §3.1](015-puesta-en-marcha-real/spec.md). El más urgente
es **P7**: rotar la API key de `nan.builders` que quedó expuesta en el chat de diseño.

Siguiente número libre: **033**. La Fase 4 (autonomía por política, Jira, memoria,
métricas) está esbozada en la [spec de 001 §13](archivo/001-diseno-base/spec.md) y
aún no tiene iniciativa.
