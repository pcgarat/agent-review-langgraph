Última modificación: 2026-10-16 | Creado: 2026-10-11

# Plan: Fase 3-E7 (desacople fuerte de la verificación)

Cierra el punto que la Fase 3 dejó explícito: **el runner síncrono no cumple E7**
(el worker, secuencial, sigue bloqueado ~20 min por fix). No es cosmético y no se
finge en el adaptador. La especificación completa vive en
`docs/iniciativas/archivo/004-fase3-e7-desacople/spec.md` y las tareas en `docs/iniciativas/archivo/004-fase3-e7-desacople/todo.md`.

## Overview

Separar la verificación de la ruta de la review con un **handoff durable por
`interrupt`**: el grafo **despacha** el fix (propone y valida localmente), publica
los hallazgos y **pausa**; un **worker de fixes** dedicado solo llama al sandbox y
encola una resolución; el worker de reviews reanuda y publica la sugerencia como
**comentario de seguimiento**. El contrato del runner (`POST /verify` bloqueante e
idempotente) **no cambia**: cambia quién espera.

## Decisiones de arquitectura

- **Un único escritor en el PR.** El worker de fixes no publica; el worker de
  reviews publica la sugerencia al reanudar. Mantiene el modelo de "una pluma" de
  `docs/adr/0006-publicacion-inline-primero-resumen-al-final.md` (B6/Q1).
- **Un único invocador del grafo.** El worker de fixes no toca el checkpointer:
  encola la resolución y reanuda el worker de reviews (misma cola única que el
  `resume` humano, D4). Evita dos procesos invocando el mismo `thread_id`.
- **El fixer y la validación se quedan donde viven hoy** (worker de reviews: tiene
  `VcsPort` y el presupuesto de tokens); el sandbox se va al worker de fixes.
- **El bucle de reintento cruza la frontera**: `fix_wait` recibe `failed` con los
  `stderr_tail` y re-propone; la pausa ocurre entre intentos, no dentro de un nodo.
- **Pausa kind-aware.** `PausedReview` gana `kind` (`human`/`fix`) con default
  `human` (compatible con las entradas en vuelo); TTL, sweep, reconcile, prune y el
  estado de la API se ramifican por `kind`. Una pausa de fix **no** se reanuda con
  `expired` humano.
- **Poda de pausas de fix.** Un hilo en `fix_wait` no puede quedar protegido para
  siempre: `FixWaitRetention` (Strategy, reusa `RetentionPolicy` de T10) lo poda
  cuando la wave expiró y la review quedó `NOT_VERIFIABLE` publicada.

## Dependency graph

```
F1–F10 (cerrado)                                   Fase 3 actual
   └── F14  contrato (FixStatus.PENDING, VerifyJob,
            FixWaitRequest/Resolution, ReviewRequest.fix_resolution)
            ├── F15  PausedReview kind-aware + PausedStore
            │        └── F16  grafo fix_dispatch / fix_wait / publish_fix / notify_fix
            │                 └── F17  worker de reviews (on_paused/resume/sweep/reconcile)
            ├── F18  FixQueue (cola de fixes + DLQ)
            │        └── F19  servicio fix-worker + Compose + settings
            │                 └── F20  idempotencia y durabilidad del handoff
            └── F21  poda de pausas de fix (FixWaitRetention)
                     └── F22  render y dedupe (:fix)
                              └── F23  E2E con fakes
                                       └── F24  docs + README + plan
```

## Task list

Ver `docs/iniciativas/archivo/004-fase3-e7-desacople/todo.md` (F14–F28). Orden por dependencia; el contrato (F14)
primero y el E2E (F23) antes de tocar docs. F25–F28 cierran los hallazgos de la revisión del PR #3.

## Checkpoints

- **Tras F14–F18:** `make ci` en verde; el contrato y el grafo despachan/pausan sin
  tocar el sandbox (fakes). La ruta de la review no contiene `SandboxPort.verify`.
- **Tras F19–F22:** `fix-worker` levanta en compose, verifica con el fake de runner
  y el worker de reviews reanuda y publica la sugerencia sin duplicar.
- **Tras F23:** E2E de fakes: review → pausa por P6 → aprobación → despacho →
  verificación (fake) → sugerencia verificada; y el caso "runner caído" → TTL →
  `NOT_VERIFIABLE`. `make ci` en verde.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| Dos invocadores del mismo hilo | El worker de fixes no reanuda: encola la resolución (un solo `invoke`) |
| Resolución aplicada a un hilo que ya avanzó | Check de `run_key` en la wave pendiente antes del resume; si no, no-op |
| Pausa de fix tratada como humana | `kind` explícito + tests por transición (§8 del spec) |
| Fixes colgados si el runner/worker caen | `run_key` idempotente + `FIX_VERIFY_TTL_S` → `NOT_VERIFIABLE` |
| Hilo en `fix_wait` inmortal (protegido de la poda) | `FixWaitRetention` (F21) |
| Regresión de HITL por el cambio de `PausedReview` | Default `human` + tests de Fase 2 intactos (F15) |

## Dependencia externa

El smoke real (criterio 9 del spec) sigue bloqueado por **P6** (revisión de
seguridad del tema protegido), como el resto de la Fase 3. F24 (docs) ya está
cerrada: no es el smoke, es el cierre documental del handoff.
