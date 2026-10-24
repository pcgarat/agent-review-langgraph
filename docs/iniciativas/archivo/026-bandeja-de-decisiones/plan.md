Última modificación: 2026-10-24 | Creado: 2026-10-24

# Plan: bandeja de decisiones

El qué está en [`spec.md`](spec.md). La decisión de diseño, en
[ADR-0030](../../../adr/0030-bandeja-web-de-la-decision.md).

## 1. Overview

Se reutiliza el inbox, el sello y `build_decision`. Lo nuevo es la lectura para
el panel, el aviso `withdraw`, el borrado del mensaje en Teams y la página que
sondea.

## 2. Decisiones de implementación

| # | Decisión | Motivo |
|---|---|---|
| P1 | `DecisionSubmitter.submit_items` para una decisión ya armada | La bandeja no marca hallazgo a hallazgo en Redis: manda el lote de una vez, como el formulario del detalle |
| P2 | `origin` en el log (`Teams`, `bandeja`, `panel`) | El mismo método encola las tres; el log no puede decir siempre «desde Teams» |
| P3 | La página pinta con `textContent` | Los títulos vienen del PR |
| P4 | Un solo sondeo en todas las páginas, y la bandeja escucha el evento | No hace falta un segundo poll para pintar las tarjetas |

## 3. Orden

1. `withdraw` en `ChatNotice` y retirada en el submitter.
2. Borrar la tarjeta en el transporte y tratar una pausa sellada como ya no vigente.
3. Servicio y endpoints de la bandeja; el detalle comparte el submitter.
4. Página, aviso y notificación.
5. Documentos vivos.

## 4. Checkpoints

- **B-API:** listar, responder, sello y `withdraw` cubiertos por tests.
- **B-Bot:** el aviso `withdraw` borra, una pausa sellada no republica, el barrido no toca una pregunta abierta.
- **B-UI:** `/bandeja` explica si Teams está configurado y no usa `innerHTML`.

## 5. Riesgos

El borrado depende de que el SDK exponga `conversations.delete_activity`. Si
Teams responde 404, se da por borrado. Un fallo de red deja la referencia y el
barrido reintenta.
