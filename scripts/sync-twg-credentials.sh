#!/usr/bin/env bash
# sync-twg-credentials.sh - Atajo de DESARROLLO: copia al `.env` la credencial de
# Bitbucket que `twg` ya tiene en el host (sesion OAuth + `bbc-token` de la persona).
#
# NO es el actor de servicio que describe docs/gestion-de-secretos-20261011.md: en
# un despliegue real `TWG_BBC_TOKEN` debe ser un API token scoped de un actor de
# servicio. Aqui se reutiliza la sesion de twg porque no hay produccion y la cuenta
# es de pruebas.
#
# Idempotente y silencioso: nunca imprime el valor de un secreto, solo las claves
# de `.env` que ha actualizado. Reutiliza TWG_SITE del `auth.conf` de twg.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly REPO_ROOT
readonly ENV_FILE="${REPO_ROOT}/.env"
readonly TWG_CONF="${HOME}/.config/twg/auth.conf"

ok()   { printf '  [ok]    %s\n' "$1"; }
warn() { printf '  [aviso] %s\n' "$1"; }
miss() { printf '  [falta] %s\n' "$1"; }

has_cmd() { command -v "$1" >/dev/null 2>&1; }

# Escribe KEY=VALUE en `.env` sin pasar el valor por argv ni por stdout (podria
# acabar en `ps` / en el historial). Reescribe sobre el mismo inodo para no perder
# los permisos 600.
set_env_var() {
  local key="$1" value="$2" tmp
  if [ -z "$value" ]; then
    warn "$key sin valor en twg; no toco .env"
    return 0
  fi
  tmp="$(mktemp)"
  chmod 600 "$tmp"
  { grep -vE "^[[:space:]]*${key}[[:space:]]*=" "$ENV_FILE" 2>/dev/null || true; } > "$tmp"
  printf '%s=%s\n' "$key" "$value" >> "$tmp"
  cat "$tmp" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  rm -f "$tmp"
  ok "$key actualizado en .env"
}

printf '\n== sync-twg-credentials: credencial de Bitbucket de twg -> .env ==\n\n'

if ! has_cmd twg; then
  miss "twg no esta en el PATH"
  exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
  miss ".env no existe; ejecuta antes scripts/bootstrap-env.sh"
  exit 1
fi

# El export va a un fichero temporal 600 y se hace `source`: los valores entran
# como variables de entorno, nunca como argumentos de un comando.
TMP_EXPORT="$(mktemp)"
chmod 600 "$TMP_EXPORT"
trap 'rm -f "$TMP_EXPORT"' EXIT
twg env auth --export --no-snapshot > "$TMP_EXPORT"
# shellcheck disable=SC1090
. "$TMP_EXPORT"

set_env_var TWG_BBC_TOKEN "${TWG_BBC_TOKEN:-}"
set_env_var TWG_USER "${TWG_USER:-}"

# TWG_SITE no lo emite el export; se toma del auth.conf de twg.
site="$(grep -E '^site=' "$TWG_CONF" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
set_env_var TWG_SITE "$site"

printf '\nListo. Recrea los contenedores para inyectar la credencial:\n'
printf '  docker compose up -d --force-recreate api worker\n\n'
