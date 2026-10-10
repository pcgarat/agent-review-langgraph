# Todo: Fase 1 — Report-only

Checklist operativa. Detalle y criterios en `tasks/plan.md`.

## Slice A — Foundation

- [ ] **T1** Clamp report-only (`max_autonomy: report` + tests)
- [ ] **T2** Contrato versionado en `contracts/` + validación
- [ ] **T3** Cola Redis: API enqueue tras idempotencia
- [ ] **T4** Worker consume cola e invoca el grafo

### Checkpoint A

- [ ] `uv run pytest` verde
- [ ] Webhook → cola → grafo → publish (VCS fake)
- [ ] Ningún `Decision` con `fix_*`

## Slice B — Review sin LLM + inline

- [ ] **T5** `TwgVcsAdapter`: scope + `resolve_inline_anchor` + `post_inline_comment`
- [ ] **T6** Persona `tooling` / `standards` (parser + fixture)
- [ ] **T7** Publish: **inline por hallazgo** + resumen PR + Teams (tope 20, P0→P3)

### Checkpoint B

- [ ] Fixture → hallazgos → ≥1 inline (o fallback documentado al resumen)
- [ ] Resumen general + Teams presentes
- [ ] Cero push/commit en VCS
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
