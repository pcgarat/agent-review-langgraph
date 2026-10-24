# ADR-0028 — El CLI de Claude corre fuera del worker

- **Estado:** Aceptado
- **Fecha:** 2026-10-23
- **Corrección:** 2026-10-23 — el enlace de la iniciativa apunta a `archivo/` tras cerrarla.

## Contexto

La empresa ya paga Claude Code. [025](../iniciativas/archivo/025-revisor-claude-cli/spec.md)
quiere un segundo motor, elegido al arrancar, en el que un solo `claude` produce
los hallazgos y el grafo conserva anclaje, la pausa y la publicación.

Dos decisiones anteriores cierran el sitio obvio donde meter ese CLI:

- [ADR-0002](0002-contrato-de-ce-code-review-sin-su-runtime.md) descartó invocar
  `claude --print` dentro del núcleo: es una capa opaca y no hay contrato estable
  que probar. El núcleo valida hallazgos contra `contracts/findings-schema.json`.
- [ADR-0018](0018-workspace-de-lectura-en-el-runner.md) dejó las lecturas en un
  clon *bare* del `sandbox-runner`. Quien publica en el PR no tiene credencial de
  clonado, y un árbol de trabajo ejecutaría *hooks* y filtros.

Claude necesita un árbol para revisar, y una review dura minutos. Un reintento de
nodo no puede repetir esa llamada. El proceso que publica no puede ser el que
ejecuta el CLI: la imagen del worker lleva el token con el que se comenta el PR.

## Decisión

1. **Servicio distinto del worker y del `sandbox-runner`.** El checkout es un
   árbol de trabajo en su propio volumen. No es el clon *bare* de ADR-0018 y no
   vive en el worker.
2. **La credencial de clonado no pasa al subproceso `claude`.** Ese subproceso
   no recibe token de VCS ni de Teams, ni herramientas de escritura. La única
   salida que cuenta es stdout. El wrapper la valida contra
   `contracts/findings-schema.json` y descarta el resto. Claude no elige la ruta
   del artefacto ni habla con el PR.
3. **El núcleo sigue viendo hallazgos ya validados.** El worker persiste el
   artefacto y solo entonces arranca el grafo. Esto no reescribe el 0002 ni el
   0018: el grafo no invoca el CLI.
4. **`REVIEW_ENGINE=llm|claude`, leído al arrancar.** Con `llm` se compila el
   grafo de hoy. Otro valor aborta el proceso. No hay interruptor por PR.

## Consecuencias

- Un reintento del grafo no relanza Claude: o hay artefacto válido para ese
  `head_sha`, o el job no entra al grafo.
- Un JSON inválido no es una review de cero hallazgos.
- `docker compose up` sin el perfil de este servicio sigue siendo el motor
  `llm`. El contenedor del CLI no recibe el token con el que el worker publica.
- El secreto de sesión de Claude no entra en `ReviewState` ni en el aviso de
  Teams. Sin sesión, el job no ocupa el hilo del worker.
- Con `claude` se compila otro grafo. No cae al de personas.

## Alternativas descartadas

- **`claude --print` dentro de un nodo.** Es lo que ADR-0002 sacó del núcleo. Un
  reintento de LangGraph repetiría una llamada de minutos.
- **Subproceso dentro del worker, con el entorno filtrado a mano.** La imagen
  lleva el token de publicación. Olvidar una variable cuela la credencial.
- **Reutilizar el volumen *bare* del `sandbox-runner`.** Mezcla el contrato de
  lectura (objetos de git, sin árbol) con un checkout de trabajo.
- **Reescribir el 0002 o el 0018.** El núcleo no cambia de contrato: sigue
  recibiendo hallazgos validados. El clon *bare* sigue sirviendo lecturas.
