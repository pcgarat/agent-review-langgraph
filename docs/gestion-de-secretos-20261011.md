Última modificación: 2026-10-11 | Creado: 2026-10-11

# Gestión de secretos del agente

Contrato, entrega y rotación de los secretos de `reviews`. Es la referencia para
saber **qué secreto existe, quién lo posee, dónde vive y cómo se sustituye**. El
principio que lo ordena es 12-factor: **la aplicación solo lee variables de
entorno**; cómo llegan esas variables es un asunto de infraestructura.

## 1. El problema, en dos partes

Mezclar estas dos cosas es lo que hace que la gestión de secretos parezca un lío:

1. **Entrega de secretos** (infraestructura): acercar el material secreto al
   proceso sin versionarlo en claro ni dejarlo legible en el host.
2. **Identidad de `twg`** (diseño): con *qué actor* habla el agente con Bitbucket y
   Jira. Un OAuth de persona no sirve para algo desatendido.

## 2. Contrato de secretos

| Variable | Qué es | Obligatorio | Fase | Dueño |
|---|---|---|---|---|
| `TWG_BBC_TOKEN` | API token scoped de Bitbucket del **actor de servicio** | Sí (`VCS_PROVIDER=twg`) | 1 | Agente |
| `TWG_SITE` | site de Atlassian (p. ej. `cqm.atlassian.net`) | Sí (`VCS_PROVIDER=twg`) | 1 | Agente |
| `TWG_USER` | usuario del actor de servicio | Sí (`VCS_PROVIDER=twg`) | 1 | Agente |
| `GITHUB_TOKEN` | PAT fine-grained de GitHub (`pull_requests: write` + `contents: read`) | Sí (`VCS_PROVIDER=github`) | 1 | Agente |
| `TOOLING_WEBHOOK_SECRET` | HMAC-SHA256 que firma el `POST /webhook/tooling` | Sin él, review degradada | 1 | Agente **y** CI |
| `TEAMS_WEBHOOK_URL` | Incoming Webhook de Teams | Si `TEAMS_ENABLED=true` | 1 | Agente |
| `TWG_TOKEN` | API token de Atlassian para `twg jira` | Al escalar a workitems | 4 | Agente |
| `LLM_API_KEY` | clave del proveedor LLM | Si `LLM_REVIEWER_ENABLED=true` | 2 | Agente |
| `POSTGRES_PASSWORD` | credencial de Postgres | En producción | 1 | Infra |
| `SANDBOX_REGISTRY_USER` / `_TOKEN` | `pull` de la imagen de CI | Al ejecutar tests | 3 | Infra |

`require_production_secrets` (en `agent_review.settings`) hace **fail-closed**: con
`ENVIRONMENT=production` el arranque falla si falta la credencial del proveedor de
VCS activo (`TWG_BBC_TOKEN` con `VCS_PROVIDER=twg`, `GITHUB_TOKEN` con `github`), si
`TOOLING_WEBHOOK_SECRET` sigue en `change-me`, o si se activaron Teams/LLM sin su
credencial. Un despliegue no puede arrancar medio configurado.

> `BITBUCKET_WEBHOOK_SECRET` existía del webhook de PR, **retirado** en favor de la
> ingesta diferida. Nadie lo consumía: se ha eliminado.

## 3. Identidad de `twg`: cuenta de servicio, no OAuth personal

Decisión: el agente autentica con un **API token scoped de un actor de servicio**,
inyectado por entorno (`TWG_BBC_TOKEN`). Verificado que `twg bb` lo usa **sin
`auth.conf`** (en un `HOME` limpio, la llamada sale en modo `bbc-token`).

Por qué no el OAuth de una persona con `~/.config/twg` montado:

- **Caduca (~1 h).** El contenedor, con el fichero en solo-lectura, no puede
  refrescarlo; depende de que alguien ejecute `twg auth refresh` en el host.
- **Atribución.** Los comentarios saldrían a nombre de una persona, no del bot.
- **Revocación.** Revocar el acceso a esa persona deja al bot ciego.
- **Portabilidad.** El token por entorno es el mismo mecanismo en local y en prod;
  no depende de un fichero que "por casualidad" existe en el host.

**Scopes mínimos:** Fase 1 → `read` y `write` de pull requests (leer diff,
comentar). Fase 3 (push de fixes) → añade escritura de ramas. Jira → `write:jira-work`.

## 4. Entrega de secretos (`.env` en el host)

La aplicación solo lee variables de entorno; el `.env` es el mecanismo de entrega.
En un despliegue de un solo host con compose, basta y no añade dependencias:

```bash
scripts/bootstrap-env.sh        # crea .env desde .env.example y lo deja a 600
$EDITOR .env                    # rellena los valores reales
docker compose up -d --build    # compose inyecta el .env en api y worker
```

Guardarraíles que lo hacen seguro:

- **`.env` fuera de git** (`.gitignore`) y **fuera de la imagen** (`.dockerignore`):
  el contenedor no lleva el fichero dentro, lo recibe por `env_file`.
- **Permisos `600`**: `bootstrap-env.sh` los aplica; solo su dueño lo lee.
- **Fail-closed**: `require_production_secrets` corta el arranque en `production` si
  falta un secreto obligatorio o sigue en su valor de ejemplo.
- **Copia por canal seguro** al añadir una máquina: nunca por el repo ni por chat.

Cuando el `.env` deja de bastar —varios entornos, varios operadores o despliegue
desde CI— es el momento de un **gestor de secretos** (Infisical/Doppler/Vault), que
los sirve al proceso sin fichero intermedio. Hoy no lo es: un host, un operador y
seis variables no lo justifican.

## 5. Higiene

- `.env` con permisos `600` (`bootstrap-env.sh` lo aplica) y en `.gitignore`.
- **Nunca** imprimir valores en logs; `require_production_secrets` reporta el
  *nombre* que falta, jamás el valor.
- Contraseña de Postgres parametrizable (`POSTGRES_PASSWORD`); Redis, sin auth, solo
  accesible en la red interna de compose.

## 6. Rotación

Guardrails concretos por tipo de credencial:

| Secreto | Cadencia | Procedimiento |
|---|---|---|
| `TWG_BBC_TOKEN` | 90 días o ante sospecha | Nuevo token scoped del actor de servicio → actualizar `TWG_BBC_TOKEN` en `.env` → `docker compose up -d worker` |
| `TOOLING_WEBHOOK_SECRET` | 90 días | **Debe rotarse a la vez en el agente y en el CI**: secretos simétricos compartidos, no hay rotación sin coordinar los dos lados |
| `TEAMS_WEBHOOK_URL` | Al revocar el conector | Regenerar el Incoming Webhook en Teams → actualizar secreto |
| `LLM_API_KEY` | Según proveedor | Rotar en el proveedor → actualizar secreto |
| `POSTGRES_PASSWORD` | 180 días | `ALTER ROLE` + actualizar secreto + recrear contenedores |

La rotación es **manual y documentada** en v1. Automatizarla (vía gestor externo)
queda para cuando exista un entorno de despliegue real.

## 7. Estado en este repo

- Credencial de `twg` por entorno, sin montar `~/.config/twg`; **verificado** que
  `twg` la usa sin `auth.conf`.
- `require_production_secrets` cubre el contrato completo (fail-closed).
- Eliminado `BITBUCKET_WEBHOOK_SECRET`; `.env` a `600`; Postgres parametrizado.
- Pendiente (infraestructura, no código): crear el actor de servicio en Atlassian y
  rellenar `TWG_BBC_TOKEN`/`TWG_SITE`/`TWG_USER` en el `.env` del host de despliegue.
