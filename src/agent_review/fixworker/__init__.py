"""Worker de fixes (Fase 3-E7): cola -> sandbox -> reanudacion.

Proceso aparte del worker de reviews. No construye el grafo, no habla con el
checkpointer y no tiene `VcsPort`: consume la wave de verificaciones que el
worker de reviews dejo en `reviews:fixes`, la resuelve con el `sandbox-runner` y
encola de vuelta el `ReviewRequest` con la `FixResolutions` para que el worker de
reviews reanude el hilo. Solo publica el worker de reviews (un unico escritor).
"""
