# Todo: Fase 1 — Report-only

Checklist operativa. Detalle y criterios en `tasks/plan.md`.

## Slice A — Foundation

- [x] **T1** Clamp report-only (`max_autonomy: report` + tests)
- [x] **T2** Contrato versionado en `contracts/` + validación
- [x] **T3** Cola Redis: API enqueue tras idempotencia
- [x] **T4** Worker consume cola e invoca el grafo
- [x] **T10** Ingesta diferida: `/webhook/tooling` + `PendingReview` + TTL; webhook de PR retirado — **puente desechable** (`docs/ingesta-vs-ejecucion-20261011.md`), no endurecer

### Checkpoint A

- [x] `uv run pytest` verde
- [x] Webhook de tooling → cola → grafo → publish (VCS fake)
- [x] Ningún `Decision` con `fix_*`
- [x] Review encolada solo con artefactos completos; espera vencida → review degradada

## Slice B — Review sin LLM + inline

- [x] **T5** `TwgVcsAdapter`: `resolve_inline_anchor` + `post_inline_comment` (+ tests goldens parciales)
- [x] **T6** Persona `tooling` / `standards` (parser + fixture + E2E)
- [x] **T7** Publish: **inline por hallazgo** + resumen PR + Teams (tope 20, P0→P3)
- [x] **T6b** Wiring de `ToolingReviewer` en el worker (`ToolingOutputSource` + perfil)

### Checkpoint B

- [x] Fixture → hallazgos → ≥1 inline (o fallback documentado al resumen)
- [x] Resumen general + Teams presentes
- [x] Cero push/commit en VCS
- [ ] Formato inline revisado por humano

## Slice C — Señal + smoke

- [x] **T8** Persona LLM `correctness` (modelo inyectable, parseo seguro)
- [x] **T9** Smoke E2E documentado + README “Fase 1” (verificar inlines en Bitbucket)

### Checkpoint C — Fase 1 hecha

- [x] Hallazgos anclados en el diff (+ resumen/Teams)
- [x] Cero escritura de código en el repo del PR
- [ ] Decisión go/no-go para Fase 2 (HITL Opción B)  ← decisión humana
- [ ] Nivel 2 del smoke ejecutado contra Bitbucket real (requiere twg + secretos)

## Fuera del repo (no bloquea la Fase 1)

- [ ] **A1 (opcional)** El CI de `emotivoo-v2` hace `POST /webhook/tooling` firmado — sin él se pierde la anotación inline de un pipeline rojo, no la Fase 1 (`docs/ingesta-vs-ejecucion-20261011.md`)
- [ ] **A2** Arranque reproducible: `bin/twg` + `.env`
- [ ] **A3** Smoke Nivel 2 una vez (verifica el contrato real de `twg` 1.3.5)

## Camino crítico mínimo (si hay que acortar)

Orden sugerido: **A2 → T1 → T3 → T4 → T6 → T7** (FakeVcs con anclas) → T5/T8 → **A3** con twg real. A1 es opcional.

## Preguntas abiertas (bloquear si no hay respuesta)

- [ ] ¿PR real de emotivoo-v2 o solo fixtures para el smoke?
- [ ] ¿Provider/modelo para T8?
- [ ] ¿Webhook Teams listo?
- [ ] ¿Clamp en `autonomy.yaml` o solo en settings de deploy?
- [x] ¿El CI publica el artefacto en el agente o se mantiene la subida a Bitbucket? → **Artefacto = puente desechable; el CI puede publicarlo (A1 opcional), pero la Fase 1 no depende de ello** (`docs/ingesta-vs-ejecucion-20261011.md`)
- [x] `max_inline_comments` = **20** (confirmado)
