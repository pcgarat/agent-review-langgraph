Última modificación: 2026-10-17 | Creado: 2026-10-17

# Todo: decisión humana desde el panel

Checklist accionable. El diseño está en [`spec.md`](spec.md); el orden y los
checkpoints, en [`plan.md`](plan.md). Spec aprobada el 2026-10-17. Una tarea cerrada se
marca `[x]` en el **mismo commit** que el código. Los IDs son estables dentro del repo:
`DH` (decisión humana).

---

## Bloque 1 — Lectura tipada (checkpoint C1)

- [x] **DH1** `PausedItem.pending` es `list[PendingDecision]` (`web/read_model.py`)
  - `_paused_item` deja de hacer `model_dump` y pasa `approval.pending` tal cual.
  - Aceptación: el read model serializa igual a JSON (`/api/state` no cambia de forma).
- [x] **DH2** Actualizar los fakes de test que construyen `PausedItem(pending=[{...}])`
  (`test_web.py`, `test_web_ui.py`) a `PendingDecision`.

## Bloque 2 — Comando (checkpoint C2)

- [x] **DH3** `CommandEffect.DECIDED` y `CommandService.decide(review_key, *, actor, items)`
  - Construye con `build_decision`; `CommandError` si no está pausada, no es humana o
    la decisión no cubre exactamente los pendientes.
  - Encola `paused.request.model_copy(update={"decision": decision})`.
- [x] **DH4** Añadir `decide` al `Protocol ReviewCommands` y al `build_command_service`.
- [x] **DH5** Tests del comando: cubre los pendientes → encola con `actor`; falta uno →
  `CommandError`; `expired` a mano → `CommandError`; pausa de fix → `CommandError`.

## Bloque 3 — Endpoint (checkpoint C3)

- [x] **DH6** `POST /api/reviews/{review_key}/decision` (`web/main.py`)
  - `require_command_session`; cuerpo `{"items": [...]}`; actor de la sesión.
  - `400` cuerpo mal formado; `409` `CommandError`; `503` si Redis falla.
- [x] **DH7** Tests HTTP: `200` con `effect=decided` y actor de sesión; el `actor` del
  cuerpo no suplanta; `403` para `lector`; `409` incompleta.

## Bloque 4 — UI (checkpoint C4)

- [x] **DH8** `_paused_detail` pinta por pendiente: severidad, título, `file:line`,
  `why_it_matters`, acción propuesta y enlace a `code_url` (escapado).
- [x] **DH9** Formulario de decisión por pendiente (`Aprobar`/`Rechazar`/`Anotar` + nota)
  y botón «Decidir» que hace `POST` con los `items` y muestra el `409` del servidor.
- [x] **DH10** Retirar `RESUME` de `_pause_commands` y de `_trace_commands` para pausas
  humanas (deja «Decidir» y «Cancelar la review»).
- [x] **DH11** Tests de UI: el detalle humano muestra los campos y el formulario; no
  aparece «Reanudar»; el formulario lleva el `data-endpoint` correcto y escapa la nota.

## Bloque 5 — Cierre (checkpoint C4)

- [x] **DH12** [`operacion.md`](../../../operacion.md) §5: fila del endpoint de decisión
  del panel.
- [x] **DH13** [`CONTEXT.md`](../../../../CONTEXT.md) si el término «decidir desde el
  panel» necesita matiz (hoy «decisión humana» ya existe; probablemente sólo un enlace).
- [x] **DH14** `make ci` y `make docs-check` en verde; marcar la iniciativa `implementada`
  en [`docs/iniciativas/README.md`](../../../README.md).
