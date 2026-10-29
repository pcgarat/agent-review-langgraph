Última modificación: 2026-10-29 | Creado: 2026-10-29

# Todo: el panel enseña los aparcados

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden, en
[`plan.md`](plan.md).

## Bloque L — Lectura (checkpoint L-K)

- [x] **L1** `parked_entries()` en el puerto de cola y en `RedisReviewQueue`: `(motivo, request)`
- [x] **L2** `ParkedItem` en `read_model.py` con `reason` tipado desde `PARK_REASONS`
- [x] **L3** `PanelState.parked` y `_read_parked` por `_guard`
- [x] **L4** Tests con `fakeredis`: motivo, orden, payload ilegible y Redis caído

## Bloque A — API (checkpoint A-K)

- [x] **A1** `GET /api/state` devuelve `parked` con sus seis campos
- [x] **A2** Tests: con parking, vacío y con el read model caído
- [x] **A3** `docs/arquitectura.md` §5: el panel lee el parking

## Bloque P — Pantalla (checkpoint P-K)

- [x] **P1** `ParkedSection` con una fila por aparcado y su acción por motivo
- [x] **P2** La cifra de «en cola» cuenta los aparcados y lo dice en el detalle
- [x] **P3** Tests: sección vacía, cada motivo con su acción, y el enlace a `/claude`
- [x] **P4** `docs/operacion.md` §6: fila de diagnóstico para «lancé una review y no aparece»
