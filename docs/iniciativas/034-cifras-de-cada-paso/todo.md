Última modificación: 2026-10-29 | Creado: 2026-10-29

# Todo: cada paso dice cuántos hallazgos entraron, salieron y por qué

## Bloque A — dominio y worker

- [x] **CF1** `DiscardedFinding` y `ReviewerResult.discarded`; allowlist del serde
- [x] **CF2** `adapt_agent_mode` devuelve los descartes estructurados; `reasons` derivado
- [x] **CF3** El worker guarda `<sha>.discarded.json`; `forget` lo borra
- [x] **CF4** `ClaudeArtifactReviewer` devuelve los descartes de contrato y de L0 con motivo
- [x] **CF5** Canal `discarded` en `ReviewState`, escrito por `persona`

Checkpoint: un artefacto como el de la PR 27 deja un descarte `l0` en el estado.

## Bloque B — traza

- [x] **CF6** `TraceNode.summary` y `TraceNode.discarded`
- [x] **CF7** Resumidores de `scope`, `persona`, `merge`, `stamp` y `publish`

Checkpoint: la traza de un grafo en memoria lleva las cifras; un checkpoint viejo se lee.

## Bloque C — panel

- [x] **CF8** Tipos de `TraceNode` y texto de cada paso en `steps.ts`
- [x] **CF9** Descartes en un `<details>` en `StepsPanel.tsx`

Checkpoint: `make frontend-check` en verde.

## Bloque D — docs y cierre

- [x] **CF10** `docs/arquitectura.md` §3–4 y `CONTEXT.md` si hace falta
- [ ] **CF11** Desplegar y comprobar con la PR 27

Checkpoint: `make ci` en verde.
