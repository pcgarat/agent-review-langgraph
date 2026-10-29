# ADR-0035 — El hijo del runner no corre como root

- **Estado:** Aceptado
- **Fecha:** 2026-10-29

## Contexto

El runner guarda en su entorno los tokens con los que trabaja: el de clonado
(`CLAUDE_GIT_TOKEN`, o el de VCS si aquel está vacío) y el token compartido del
servicio (`CLAUDE_RUNNER_TOKEN`). `_drop_secret_env()` los borra de la copia en
memoria del proceso, y el comentario que lo acompañaba afirmaba que así un `Read`
de `/proc/1/environ` no los veía.

No es cierto. El kernel conserva el entorno de arranque del proceso, y
`/proc/<pid>/environ` lo expone mientras el proceso viva. Comprobado en la imagen:
el fichero es `400 root`, el runner es PID 1 con uid 0, y el hijo `claude` hereda
ese mismo uid. El resultado es que una inyección de prompt puede pedir un `Read`
de `/proc/1/environ` y sacar los tokens en el texto de la review, que después se
publica en el pull request.

Las reglas del sandbox no cierran este camino: `Read`, `Grep` y `Glob` corren en
el proceso `claude`, fuera de `bwrap`. Un `denyRead` del sandbox no las alcanza.
Negar `Read` por el argv tampoco vale: la skill lo necesita para revisar.

## Decisión

**El subproceso `claude` se ejecuta con un uid sin privilegios (`nobody`).**

El drop ocurre en el hijo, entre `fork` y `exec` (`preexec_fn`), así que el
runner sigue siendo root para clonar, firmar y escribir su estado. Un proceso sin
privilegios no puede abrir `/proc/1/environ`; lo niega el kernel, no una regla del
CLI ni del sandbox. Es la única defensa de las planteadas que no depende de que el
modelo obedezca.

Consecuencias mecánicas, todas dentro de `POST /run`:

- La config del CLI (`CLAUDE_CONFIG_DIR`, con la skill volcada) se cede a `nobody`.
- El hijo recibe un `HOME` y un `TMPDIR` propios dentro de esa config, porque el
  `HOME` del proceso es de root y el CLI escribe ahí su estado de sesión.
- El checkout lo sigue escribiendo `git` como root. El hijo solo lo lee. El
  sandbox ya declara esa raíz como `denyWrite`.
- Si el proceso es root y el usuario no existe, el runner **no arranca**
  (`require_child_user`). Un runner que arranca y luego revisa como root es peor
  que uno que no arranca.
- El `settings.json` añade `denyRead` para `/proc/1/environ` y `/proc/1/cmdline`:
  cubre las herramientas que sí corren dentro de la jaula. Es defensa en
  profundidad, no la principal.

## Consecuencias

- El vector del P0 queda cerrado por el kernel. Comprobado con el CLI real y el
  sandbox activo: `id -u` devuelve `65534` y `cat /proc/1/environ` da
  *Permission denied*.
- `bwrap` no es setuid en esta imagen, así que el drop no pierde la capacidad de
  crear el user namespace. `claude --version` y una review completa funcionan como
  `nobody`.
- Aparece un acoplamiento nuevo: el ritual de permisos de la config. Un fichero
  que el runner cree después de `_grant_child` queda otra vez de root. La función
  se llama después de escribir la config y de copiar la skill.
- Sigue abierto lo que este ADR **no** cubre: el token de sesión viaja en el
  entorno del hijo (`CLAUDE_CODE_OAUTH_TOKEN`) y un `Bash` puede leerlo. Lo que la
  jaula impide es enviarlo fuera: solo hay ruta a `api.anthropic.com`.

## Alternativas descartadas

1. **Leer los secretos de ficheros `0400` en vez del entorno.** Cierra
   `/proc/1/environ` para todos, pero mueve el problema: el hijo necesitaría leer
   el fichero del token de clonado para el `git fetch`, y volvería a estar a su
   alcance. Además obliga a un segundo mecanismo de secretos en el despliegue.
2. **Reejecutar el runner con `execve` y un entorno limpio.** El kernel reescribe
   `/proc/1/environ`, sí, pero las variables tienen que entrar por otro sitio (otra
   vez el entorno que se quiere ocultar), y añade un arranque de dos fases al
   servicio. Más superficie para el mismo resultado.
3. **Solo `denyRead` del sandbox.** No alcanza a `Read`, que es justo la
   herramienta del vector. Da una falsa sensación de cierre.
4. **Negar `Read` por el argv.** Rompe la skill: sin `Read` no hay review.
