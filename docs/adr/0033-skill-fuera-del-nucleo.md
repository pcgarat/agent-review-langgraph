# ADR-0033 — La skill revisa fuera del núcleo

- **Estado:** Aceptado
- **Fecha:** 2026-10-24
- **Corrección:** 2026-10-24 — el contenedor de `claude-runner` no lleva el seccomp ni el AppArmor por defecto. Esos perfiles impiden el user namespace que `bwrap` necesita, y sin él cada `Bash` falla. La jaula sigue siendo el sandbox del CLI.
- **Corrección:** 2026-10-24 — dentro de Docker, `bwrap` tampoco puede montar un `/proc` nuevo. El `settings.json` activa `enableWeakerNestedSandbox`: la jaula reutiliza el `/proc` del contenedor.
- **Corrección:** 2026-10-24 — 200 turnos y 3600 s son el valor inicial. El panel puede publicar otros, entre 1 y 2000 turnos y entre 1 y 21600 s. El worker los envía en `POST /run`. Sin override, el runner sigue con su entorno.

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
   proceso no arranca. El servicio Compose declara `apparmor=unconfined` y
   `seccomp=unconfined`: con el perfil por defecto de Docker, `bwrap` no crea
   el namespace y el sandbox no llega a existir. Hacen falta los dos. Aun
   así, montar un `/proc` nuevo devuelve `Can't mount proc`. El
   `settings.json` pone `enableWeakerNestedSandbox`: la jaula reutiliza el
   `/proc` del contenedor y ve sus procesos. La lista de dominios y
   `denyWrite` no cambian.
3. **El techo no se reintenta.** El valor inicial de `CLAUDE_MAX_TURNS` es 200
   y el de `CLAUDE_TIMEOUT_S` es 3600. El panel puede publicarlos dentro de
   1–2000 turnos y 1–21600 s; el worker los manda en el cuerpo de `POST /run`.
   Agotar los turnos o el tiempo de pared parkea el job con motivo `ceiling` y
   no lo reencola. Una caída anterior a ese techo sigue siendo reintentable.
   La transcripción de esa pasada se queda y se descarga entera, con sesión
   del panel.

## Consecuencias

- Un PR puede ocupar la cola del worker hasta el tiempo de pared publicado
  (por defecto una hora, como máximo seis). Mientras corre, no coge otro job.
- El volumen `claude_session` crece con la conversación. No se recorta: llenar
  el disco es un fallo de escritura.
- La copia de la skill vive en la imagen del runner, no en el árbol del PR.
  `--setting-sources user` no carga la config de ese árbol.
- El contenedor no es la jaula de `Bash`. Un comando que saliera de `bwrap`
  queda en un proceso sin el seccomp ni el AppArmor de Docker, con los
  volúmenes del runner y sin puertos publicados.
- Con `enableWeakerNestedSandbox`, un `Bash` ve la lista de procesos del
  contenedor. No ve un `/proc` privado de la jaula.

## Alternativas descartadas

- **Denegar `Bash` por completo.** En `dontAsk` el CLI tampoco puede hacer el
  `git diff` de la base.
- **Reintentar el techo.** La misma pasada volvería a gastar el fan-out y
  chocaría con el mismo límite.
- **Parsear el markdown de la skill.** No hay contrato que probar; el objeto
  `mode:agent` ya es la salida.
- **Un perfil de seccomp y AppArmor reducido a los syscalls del user
  namespace.** Más estrecho, y se rompe en cuanto `bwrap` pide uno que no
  está listado. El cierre sigue siendo el sandbox del CLI.
