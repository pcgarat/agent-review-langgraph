Última modificación: 2026-10-11 | Creado: 2026-10-11

# Cierre de la Fase 1: registro de la auditoría y su resolución

Cuando se escribió la auditoría «qué falta para cerrar la Fase 1», el código pasaba
tests pero tenía huecos de robustez, fuentes de verdad duplicadas y decisiones de
producto sin fijar. Este documento era el **delta** entre «el código existe» y
«produce reviews útiles sin sorpresas».

**A 2026-10-11 ese delta es cero en código.** Lo que queda abierto son verificaciones
humanas o externas. Este fichero se conserva como registro de lo que se encontró y
cómo se cerró; el estado vivo está en `tasks/plan.md`.

---

## Resoluciones (antes: los pendientes accionables)

| Punto | Qué era | Cómo se resolvió |
|---|---|---|
| **A2** | El contenedor podía arrancar sin `twg`; no había forma de preparar `.env` | `scripts/bootstrap-env.sh` (idempotente) + validación de `twg --version` en el arranque (`check_twg`, `REQUIRE_TWG` en build) |
| **B1** | El publish no miraba lo ya publicado: un re-push duplicaba inlines | Marcas `<!-- reviews:<hash>:inline:<id> -->` y `:ok/partial`; lectura previa con `list_review_markers` |
| **B2** | Un fallo transitorio perdía el job (solo log) | Reintento con contador + **DLQ** (`reviews:jobs:dead`) |
| **B4** | `expected_tools` podía divergir entre perfil y env | `reviews.profile` como fuente única: manda el perfil, el env solo lo sobreescribe explícito |
| **B5** | `max_files_per_pr` declarado pero no aplicado; `get_diffstat` sin usar | `scope` filtra ruido y recorta por churn con `get_diffstat` |
| **B6** | Publicación no atómica | Orden y compensación fijados en `docs/decisiones-publish-inline-20261011.md` |
| **B7** | Sin watchdog: si el worker moría, se quedaba muerto | `restart: unless-stopped` + healthchecks + latido en hilo independiente |
| **C2** | Qué hacer con borrados puros | Decidido: caen al resumen, sin anclaje automático |
| **C3** | `budget_tokens_per_pr` solo se logueaba | Corte real: estima tokens y no llama si lo supera |
| **C4** | Heurística de tema protegido mínima | Ampliada por dominio (auth, secretos, inyección, memoria, concurrencia, cripto, contrato) |
| **D1** | Clamp `report` sin override posible | `MAX_AUTONOMY_OVERRIDE`, que **solo puede bajar** el techo del YAML |
| **D2** | Plantilla del artefacto sin validar | Validación en el arranque de API y worker |
| **D3** | Sin límite de tamaño en el `POST` | `max_tooling_payload_bytes` → **413** |
| **D4** | Secreto `change-me` omitía la firma en silencio | `require_production_secrets`: falla el arranque en producción |
| **E** | Postgres sin uso, sin CI, `mypy` con errores, Teams sin tests HTTP | CI propio + `Makefile`, `types-pyyaml`, tests HTTP de Teams; Postgres se mantiene como checkpointer reservado |

Detalle y porqués de cada uno están en el código y en el commit que lo introdujo.

---

## Lo que sigue abierto (no es código)

- **A1 / P1** — El CI de `emotivoo-v2` haga el `POST /webhook/tooling` firmado.
  **Opcional**: sin él se pierde la anotación inline de un pipeline rojo, no la Fase 1
  (`docs/ingesta-diferida-ci-20261011.md` §3–4).
- **A3 / P2** — Ejecutar el smoke Nivel 2 con `twg` y secretos reales
  (`scripts/smoke-nivel2.sh <pr_id>`).
- **C1 / P3** — Revisión humana del formato inline
  (`docs/decisiones-publish-inline-20261011.md`).
- **P4** — Decisión go/no-go para Fase 2 (HITL Opción B).
- **P5** — Medir la latencia real de una review (la cola es secuencial).
- **P6** — Revisar la heurística de tema protegido con seguridad (decide el autofix en
  Fase 3).
- **Rate-limit** explícito en `/webhook/tooling`: no bloqueante (lo acotan firma +
  tamaño del payload + ingress).
- **Observabilidad** (trazas LangSmith, métricas de coste/falsos positivos): no bloquea
  report-only.

---

## Definición de hecho de la Fase 1

- Hallazgos **anclados inline** en el diff + resumen en el PR + teaser en Teams.
- **Cero** escritura de código en el repo del PR (sin push/commit/branch).
- Ninguna `Decision.action` es `fix_*`.
- Una review real, disparada por el `POST` del CI (o por el puente equivalente),
  publicada y **revisada por una persona** que diga «esto lo leería».

El código cumple las tres primeras. Las dos últimas son el **P2** y el **P3** de
`tasks/plan.md`: con ambas en verde se puede declarar la Fase 1 hecha y decidir la
Fase 2.
