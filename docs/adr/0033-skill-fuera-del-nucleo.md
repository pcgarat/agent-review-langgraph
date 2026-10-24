# ADR-0033 — La skill revisa fuera del núcleo

- **Estado:** Aceptado
- **Fecha:** 2026-10-24

## Contexto

[ADR-0002](0002-contrato-de-ce-code-review-sin-su-runtime.md) dejó el contrato de
hallazgos en el repo y descartó invocar la skill dentro del núcleo. [ADR-0028](0028-cli-claude-fuera-del-worker.md)
sacó el CLI del worker: el núcleo sigue viendo hallazgos ya validados. Con eso, el
motor `claude` pedía un array JSON y solo dejaba leer el árbol. No es la review
que se hace en local con `ce-code-review`.

[028](../iniciativas/028-claude-revisa-con-la-skill/spec.md) cambia el prompt, las
herramientas y la conservación de la conversación. No mueve el CLI al worker ni
hace que el grafo interprete el markdown de la skill.

## Decisión

1. **La skill corre en el runner, fuera del núcleo.** El prompt es
   `/ce-code-review mode:agent base:<base_sha>`. Un adaptador puro, antes del
   grafo, traduce el objeto `mode:agent` a la lista que ya valida
   `contracts/findings-schema.json`. El grafo no llama a la skill. Esto no
   reescribe el 0002 ni el 0028.
2. **El sandbox es el cierre de `Bash`.** `Bash` no se deniega entero: en
   `dontAsk` eso quita también el git de lectura. La red y la escritura las
   cierra el sandbox del CLI: `bubblewrap` y `socat` en la imagen, lista
   estricta con solo `api.anthropic.com`, sin comandos fuera del sandbox, el
   checkout no escribible y `/tmp` sí. Si el sandbox no puede activarse, el
   proceso no arranca.
3. **El techo no se reintenta.** `CLAUDE_MAX_TURNS` es 200 y `CLAUDE_TIMEOUT_S`
   es 3600. Agotar los turnos o el tiempo de pared parkea el job con motivo
   `ceiling` y no lo reencola. Una caída anterior a ese techo sigue siendo
   reintentable. La transcripción de esa pasada se queda y se descarga entera,
   con sesión del panel.

## Consecuencias

- Un PR puede ocupar la cola del worker hasta una hora. Mientras corre, no coge
  otro job.
- El volumen `claude_session` crece con la conversación. No se recorta: llenar
  el disco es un fallo de escritura.
- La copia de la skill vive en la imagen del runner, no en el árbol del PR.
  `--setting-sources user` no carga la config de ese árbol.

## Alternativas descartadas

- **Denegar `Bash` por completo.** En `dontAsk` el CLI tampoco puede hacer el
  `git diff` de la base.
- **Reintentar el techo.** La misma pasada volvería a gastar el fan-out y
  chocaría con el mismo límite.
- **Parsear el markdown de la skill.** No hay contrato que probar; el objeto
  `mode:agent` ya es la salida.
