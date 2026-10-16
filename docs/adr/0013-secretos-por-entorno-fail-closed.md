# ADR-0013 — Secretos solo por entorno y arranque *fail-closed* en producción

- **Estado:** Aceptado
- **Fecha:** 2026-10-11

## Contexto

Hay dos problemas que se mezclaban: **cómo llegan** los secretos al proceso
(infraestructura) y **con qué identidad** habla el agente con Bitbucket (diseño, ver
[ADR-0012](0012-vcs-tras-un-puerto.md)).

## Decisión

- **12-factor:** la aplicación solo lee variables de entorno (`pydantic-settings`).
  Hoy el mecanismo de entrega es un `.env` en el host. Está fuera de git y de la
  imagen (`.gitignore`, `.dockerignore`), tiene permisos `600` (los aplica
  `scripts/bootstrap-env.sh`) y compose lo inyecta con `env_file`.
- **Arranque *fail-closed*.** Con `ENVIRONMENT=production`,
  `require_production_secrets` impide arrancar si falta:
  - la credencial del proveedor de VCS activo;
  - `TOOLING_WEBHOOK_SECRET` o `REVIEWS_DECISION_TOKEN` con un valor real (no el de
    ejemplo);
  - `TEAMS_NOTICES_KEY` si `TEAMS_ENABLED` (corregido en la iniciativa 013: la URL del
    Incoming Webhook desapareció);
  - `LLM_API_KEY` si el LLM está activo;
  - `SANDBOX_RUNNER_TOKEN` y `SANDBOX_GIT_TOKEN` si el sandbox está activo.

  El error dice el **nombre** que falta, nunca el valor.
- **Mínimo privilegio por token.** El runner clona con `SANDBOX_GIT_TOKEN` (solo
  lectura), distinto del token del worker.
- Rotación manual y documentada en v1 (ver [`operacion.md`](../operacion.md#3-secretos)).

## Consecuencias

- Fuera de producción, `change-me` **desactiva** la firma HMAC del webhook y el
  bearer de decisión y del runner. Un despliegue que olvide
  `ENVIRONMENT=production` queda abierto (R2 de la revisión de `main`, abierto).
- Los secretos simétricos (`TOOLING_WEBHOOK_SECRET` con el CI,
  `SANDBOX_RUNNER_TOKEN` entre worker y runner) se rotan a la vez en los dos lados.
- Con varios entornos u operadores, llegará un gestor de secretos (Infisical, Doppler
  o Vault) sin cambiar la aplicación.

## Alternativas descartadas

- **Gestor de secretos ya.** Un host, un operador y unas pocas variables no lo
  justifican.
