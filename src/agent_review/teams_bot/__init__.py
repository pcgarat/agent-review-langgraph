"""Canal bidireccional de Teams (iniciativa 013).

Aqui vive el servicio `teams-bot`: el render de la tarjeta de decision, el estado
por usuario y, a partir de T5/T7, el consumo de la outbox y el ingress del Bot
Framework. Nada de este paquete lo conoce el worker: el worker solo encola
`ChatNotice` en la outbox (`ChatPort`).
"""
