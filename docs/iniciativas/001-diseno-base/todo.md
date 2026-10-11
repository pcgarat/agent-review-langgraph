Última modificación: 2026-10-11 | Creado: 2026-10-11

# Todo: Fase 1 (report-only)

Checklist de la Fase 1. El plan está en [`plan.md`](plan.md) y el diseño, en
[`spec.md`](spec.md).

## Código (cerrado)

- [x] **T1–T10** entregadas (clamp report, contrato, cola, worker, adaptador vCS,
      personas estática y LLM, publish inline+resumen+Teams, smoke, ingesta diferida)
- [x] Robustez: reintentos + DLQ, healthchecks + restart, latido del worker
- [x] Dedupe contra lo publicado (marca por hallazgo y por resumen)
- [x] Scope sin ruido + `max_files_per_pr`
- [x] Arranque reproducible (`scripts/bootstrap-env.sh` + validación de `twg`/secretos)
- [x] CI propio (`.github/workflows/ci.yml`) + `make ci`: `ruff` lint y formato,
      `mypy` strict, 182 tests en verde

## Pendiente (humano / externo, no es código)

- [ ] **P1** (opcional) El CI de `emotivoo-v2` hace `POST /webhook/tooling` firmado —
      sin él se pierde la anotación inline de un pipeline rojo, no la Fase 1
- [ ] **P2** Nivel 2 del smoke contra Bitbucket real — ✅ ejecutado con el PR 430
      (`docs/operacion.md` §6)
- [ ] **P3** Formato inline revisado por humano (`docs/adr/0006-publicacion-inline-primero-resumen-al-final.md`)
- [x] **P4** Decisión go/no-go para Fase 2 → **go** (2026-10-11)
- [ ] **P5** Medir la latencia real de una review (la cola es secuencial) — medición
      orientativa hecha (~15 s); falta carga real
- [ ] **P6** Revisar la heurística de tema protegido con seguridad (Fase 3)

## Preguntas abiertas

- [ ] ¿PR real de emotivoo-v2 o solo fixtures para el smoke? (P2)
- [ ] ¿Webhook de Teams listo para el smoke? (P2)
- [x] ¿Clamp en `autonomy.yaml` o solo en settings? → **YAML, con override que solo baja**
- [x] ¿El CI publica el artefacto en el agente? → **Puente desechable; opcional** (`docs/adr/0004-ingesta-diferida-por-artefacto-del-ci.md`)
- [x] ¿`max_inline_comments`? → **20**
