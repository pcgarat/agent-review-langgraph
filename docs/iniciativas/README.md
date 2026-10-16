Última modificación: 2026-10-16 | Creado: 2026-10-11

# Iniciativas

Cada iniciativa tiene su carpeta `NNN-slug/` con `spec.md` (qué y por qué),
`plan.md` (cómo y en qué orden) y `todo.md` (qué falta). El ciclo de vida y la
nomenclatura están en la regla [`documentacion`](../../.agents/rules/documentacion.md).
Las cerradas sin pendientes se mueven a `archivo/`.

## Estado

| # | Iniciativa | Spec | Estado | Lo que queda |
|---|---|---|---|---|
| 001 | [Diseño base y Fase 1 (report-only)](001-diseno-base/spec.md) | implementada | ✅ Entregada; smoke real con el PR 430 | P1 (opcional), P3, P5 · [todo](001-diseno-base/todo.md) |
| 002 | [Fase 2 — HITL](002-fase2-hitl/spec.md) | implementada | ✅ T1–T10 cerradas | Smoke del checkpointer contra Postgres real · [todo](002-fase2-hitl/todo.md) |
| 003 | [Fase 3 — Fix + verificación](003-fase3-fix-verificacion/spec.md) | aprobada | 🟡 F1–F10 hechas | F11 (spike R1), F12 (smoke real), F13 (docs), P6 · [todo](003-fase3-fix-verificacion/todo.md) |
| 004 | [Fase 3-E7 — Desacople fuerte de la verificación](004-fase3-e7-desacople/spec.md) | implementada | ✅ F14–F28 cerradas (incl. los hallazgos de la revisión del PR #3) | Smoke real (P6) · [todo](004-fase3-e7-desacople/todo.md) |
| 005 | [Fase 3.5 — Modelos declarativos por rol](005-modelos-declarativos/spec.md) | implementada | ✅ M1–M11 cerradas (incl. los hallazgos de la revisión del PR #3) | Smoke real (P6) · [todo](005-modelos-declarativos/todo.md) |
| 006 | [Evaluación de la review: *gold set* y métricas](006-eval-de-la-review/spec.md) | borrador | 🔵 Base de medición | La miden 007–011 |
| 007 | [Contrato de verificación del hallazgo](007-contrato-de-verificacion/spec.md) | borrador | 🔵 Habilita la cascada | Depende de 006 para medirse · habilita 008, 009 y 011 |
| 008 | [Contexto del revisor: L0 determinista y L1 contexto](008-contexto-del-revisor/spec.md) | borrador | 🔵 Capas baratas de la cascada | Depende de 007 y 012 |
| 009 | [Verificador adversarial (L2)](009-verificador-adversarial/spec.md) | borrador | 🔵 Capa de pago de la cascada | Depende de 007 · se mide con 006 |
| 010 | [Personas deterministas de wiring y configuración](010-personas-de-wiring/spec.md) | borrador | 🔵 Vía de recall | Fase 1 (gate de CI) sin dependencias; fase 2 (persona) depende de 007 |
| 011 | [Gate de severidad por política y observabilidad](011-gate-de-severidad/spec.md) | borrador | 🔵 Cierre de la cascada; único dueño de la severidad | Depende de 007, 008 y 009 |
| 012 | [Cobertura de la review](012-cobertura-de-la-review/spec.md) | borrador | 🔵 Que el revisor vea el PR y diga lo que no vio | Depende de 006 para medirse · habilita 008 |

Las iniciativas **006–012** son el desglose de una única auditoría de la calidad de
la review del PR #3 (6 falsos positivos, 1 hallazgo invertido y 3 P1 no detectados):
medir (006), cobertura (012: el revisor vio ≈ 5 de 51 ficheros sin avisar), contrato
(007), las capas baratas (008), la capa adversarial (009), el recall determinista
(010) y el gate que lo cierra (011). Orden de dependencia:
006 → 012 → 007 → (008 · 009 · 010 en paralelo) → 011. La fase 1 de 010 (gate de
CI) puede ir en cualquier momento.

Siguiente número libre: **013**. La Fase 4 (autonomía por política, Jira, memoria,
métricas) está esbozada en la spec de 001 §13 y aún no tiene iniciativa.

## Pendientes humanos o externos (transversales)

| Id | Pendiente | Bloquea a |
|---|---|---|
| P1 | El CI de `emotivoo-v2` hace `POST /webhook/tooling` firmado. Opcional ([ADR-0004](../adr/0004-ingesta-diferida-por-artefacto-del-ci.md)) | — |
| P3 | Revisión humana del formato inline y del resumen ([ADR-0006](../adr/0006-publicacion-inline-primero-resumen-al-final.md)) | Dar por cerrada la 001 |
| P5 | Medir la latencia con carga real (LLM + PR grande) | — |
| P6 | Revisar con seguridad la heurística de tema protegido | Smoke real de 003 y 004 |
