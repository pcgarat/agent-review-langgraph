# Todo: Fase 1 — Report-only

Checklist operativa. Detalle y criterios en `tasks/plan.md`.

## Slice A — Foundation

- [x] **T1** Clamp report-only (`max_autonomy: report` + tests)
- [x] **T2** Contrato versionado en `contracts/` + validación
- [x] **T3** Cola Redis: API enqueue tras idempotencia
- [x] **T4** Worker consume cola e invoca el grafo

### Checkpoint A

- [x] `uv run pytest` verde
- [x] Webhook → cola → grafo → publish (VCS fake)
- [x] Ningún `Decision` con `fix_*`

## Slice B — Review sin LLM + inline

- [x] **T5** `TwgVcsAdapter`: `resolve_inline_anchor` + `post_inline_comment` (+ tests goldens parciales)
- [x] **T6** Persona `tooling` / `standards` (parser + fixture + E2E)
- [x] **T7** Publish: **inline por hallazgo** + resumen PR + Teams (tope 20, P0→P3)
- [ ] **T6b** Wiring de `ToolingReviewer` en el worker (desde el perfil/salida de CI)

### Checkpoint B

- [x] Fixture → hallazgos → ≥1 inline (o fallback documentado al resumen)
- [x] Resumen general + Teams presentes
- [x] Cero push/commit en VCS
- [ ] Formato inline revisado por humano

## Slice C — Señal + smoke

- [ ] **T8** Persona LLM `correctness` (modelo inyectable, parseo seguro)
- [ ] **T9** Smoke E2E documentado + README “Fase 1” (verificar inlines en Bitbucket)

### Checkpoint C — Fase 1 hecha

- [ ] Hallazgos anclados en el diff (+ resumen/Teams)
- [ ] Cero escritura de código en el repo del PR
- [ ] Decisión go/no-go para Fase 2 (HITL Opción B)

## Camino crítico mínimo (si hay que acortar)

Orden sugerido: **T1 → T3 → T4 → T6 → T7** (FakeVcs con anclas) → T5/T8/T9 con twg real.

## Preguntas abiertas (bloquear si no hay respuesta)

- [ ] ¿PR real de emotivoo-v2 o solo fixtures para el smoke?
- [ ] ¿Provider/modelo para T8?
- [ ] ¿Webhook Teams listo?
- [ ] ¿Clamp en `autonomy.yaml` o solo en settings de deploy?
- [x] `max_inline_comments` = **20** (confirmado)
