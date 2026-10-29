# ADR-0032 — El checkout de Claude reutiliza el token de VCS

- **Estado:** Aceptado
- **Fecha:** 2026-10-24

## Contexto

[ADR-0028](0028-cli-claude-fuera-del-worker.md) saca el CLI del worker para que
el proceso que publica no sea el que ejecuta `claude`. El fetch necesitaba otra
variable, `CLAUDE_GIT_TOKEN`, pensada como credencial de solo lectura distinta
de `GITHUB_TOKEN` o `TWG_BBC_TOKEN`.

En la práctica es una cuarta credencial que el operador tiene que inventar o
copiar, y el checkout solo sabía construir una URL de Bitbucket. Quien publica
en GitHub ya tiene un token con `contents: read`.

## Decisión

1. Si `CLAUDE_GIT_TOKEN` está vacío, el runner clona con el token de VCS del
   proveedor (`GITHUB_TOKEN` o `TWG_BBC_TOKEN`). La URL es `github.com` con
   `x-access-token`, o `bitbucket.org` con `x-token-auth`.
2. Un `CLAUDE_GIT_TOKEN` puesto sigue ganando: es el override de solo lectura
   para quien no quiera que el runner tenga el token que también comenta el PR.
3. El worker sigue recibiendo `CLAUDE_GIT_TOKEN` vacío. El runner recibe el
   token de VCS, lo usa en el `fetch`, lo retira del entorno y no se lo pasa
   al subproceso `claude`. El remoto se quita antes de arrancar el CLI.

## Consecuencias

- No hace falta otra variable para clonar el mismo repo que ya se publica.
- El proceso del runner, no el subproceso, tiene en memoria un token que
  también puede escribir en el PR. Quien quiera separarlos rellena
  `CLAUDE_GIT_TOKEN` con uno de solo lectura.
- El contrato de arranque del runner exige el token de VCS cuando el override
  está vacío, no `CLAUDE_GIT_TOKEN`.

## Alternativas descartadas

- **Seguir exigiendo `CLAUDE_GIT_TOKEN` siempre.** Aísla mejor el runner y
  obliga a configurar una credencial que, para GitHub, repite `contents: read`.
- **Copiar el token al `.env` con otro nombre al arrancar.** Sigue habiendo
  dos variables con el mismo secreto y hay que rotarlas a la vez.
- **Pasarle el token al subproceso `claude`.** Es lo que ADR-0028 prohíbe: el
  CLI no tiene que poder hablar con el PR.
