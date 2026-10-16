"""Panel de operacion (iniciativa 019): servicio `web` de lectura y comandos.

Vive en un proceso aparte, opt-in por el perfil de compose `panel` (W1), porque el
worker es un bucle tonto y la API esta en el camino critico del webhook del CI. Es
CQRS-lite (W3): lee estado de Redis y el recorrido del checkpointer, y encola
comandos; el worker sigue siendo el unico escritor del estado del grafo.

La instancia FastAPI es `agent_review.web.main:app`; el proceso se arranca con
`python -m agent_review.web.main`.
"""
