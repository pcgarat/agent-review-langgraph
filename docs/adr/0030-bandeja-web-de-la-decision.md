# ADR-0030 — La bandeja del panel es la otra superficie de la misma pregunta

- **Estado:** Aceptado
- **Fecha:** 2026-10-24

## Contexto

Una pausa humana (`interrupt` con `ApprovalRequest`) se muestra en Teams como
tarjeta si `TEAMS_ENABLED` está activo. Sin Teams, la pregunta solo se podía
responder desde el detalle de la review, el CLI o la API, y no había un aviso
cuando aparecía. Quien opera el panel quiere ver y responder esas interrupciones
en la web, con una notificación al llegar, y que la primera respuesta —de Teams
o de la web— retire la pregunta de los dos sitios.

El sello de la primera decisión (`CardStore.close`, `SETNX` por `pause_id`) ya
existía para la tarjeta. El formulario del detalle (022) no lo usaba: encolaba
por su cuenta. Con dos botones vivos, eso deja una segunda reanudación en la
cola hasta que el worker la convierte en no-op, y la tarjeta de Teams sigue
publicada.

## Decisión

1. La **bandeja** (`/bandeja`) lista las pausas humanas abiertas del inbox que
   aún no tienen sello. No es una copia del chat de Teams: los avisos
   `published` y las pausas de fix no son preguntas y no entran.
2. Responder usa el mismo `DecisionSubmitter` que la tarjeta (`build_decision`,
   sello, encolado). El formulario del detalle también pasa por él, para que
   las dos superficies de la web y Teams compartan la primera respuesta.
3. Quien gana el sello encola un `ChatNotice` de `kind=withdraw` solo si Teams
   está configurado. El bot borra el mensaje (`delete_activity`) y, al publicar
   un aviso de pausa, lo descarta si la pausa ya está sellada. Un barrido borra
   las tarjetas cuya pausa ya no está en el inbox, para el CLI, la API y el TTL.
4. Sin Teams, no se encola `withdraw` (no hay consumidor) y la bandeja es la
   única superficie. El worker no cambia: la pausa sigue registrándose en el
   inbox aunque no haya canal.
5. El panel sondea `/api/bandeja`. Una pausa nueva abre un aviso en la página y,
   con permiso del navegador y la pestaña oculta, una notificación del sistema.
   La pregunta permanece hasta que deja de estar abierta.

## Consecuencias

- La carrera entre clics deja de encolar dos reanudaciones en las superficies
  que comparten el sello. El CLI y la API siguen sin sellar; el barrido del bot
  retira la tarjeta cuando la pausa sale del inbox.
- `render_decided_card` deja de ser lo que el canal muestra al responder. Sigue
  en el render por si un cliente antiguo la pide; el ingress borra el mensaje.
- El valor de `reviews:teams:cards` guarda también `review_key`, para que el
  barrido sepa qué pausa corresponde a cada mensaje. Una entrada antigua, sin
  ese campo, solo se borra con un `withdraw` que traiga la clave.

## Alternativas descartadas

- **Duplicar el mensaje de Teams en la web.** La outbox es el transporte del
  bot, no la lista de preguntas. Sin Teams no habría nada que mostrar, y un
  aviso ya consumido desaparecería de la cola antes de que la persona abriera
  el panel. El inbox es la fuente.
- **Dejar la tarjeta como «decidido por X».** Es lo que hacía el ingress. La
  pregunta tiene que desaparecer de los dos sitios en cuanto hay respuesta.
- **Un sello nuevo solo para la bandeja.** Ya existe el de la pausa. Otro mutex
  volvería a permitir que Teams y la web encolaran los dos.
