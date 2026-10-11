#!/usr/bin/env bash
# bootstrap-env.sh - Prepara el arranque local de `reviews` (pendiente A2).
#
# Idempotente y seguro: crea `.env` desde `.env.example` solo si falta, comprueba
# las dependencias minimas del arranque (`docker`, `docker compose`, `uv`, `twg`) y
# resume que variables quedan por rellenar. Nunca imprime el valor de un secreto.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly REPO_ROOT
readonly ENV_FILE="${REPO_ROOT}/.env"
readonly ENV_EXAMPLE="${REPO_ROOT}/.env.example"
readonly TWG_IN_BIN="${REPO_ROOT}/bin/twg"

# Valores que `.env.example` deja como "sin rellenar".
readonly PLACEHOLDER_VALUES=("change-me" "changeme" "todo" "your-secret" "")

MISSING_REQUIRED=0
PENDING_VARS=0

ok()   { printf '  [ok]        %s\n' "$1"; }
warn() { printf '  [aviso]     %s\n' "$1"; }
miss() { printf '  [falta]     %s\n' "$1"; }
pending() { printf '  [pendiente] %s\n' "$1"; }

has_cmd() { command -v "$1" >/dev/null 2>&1; }

# Devuelve el valor efectivo de una clave sin expandirlo ni imprimirlo fuera de
# esta funcion. La ultima asignacion gana, como haria `docker compose`.
env_value() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  { grep -E "^[[:space:]]*${key}[[:space:]]*=" "$ENV_FILE" || true; } \
    | tail -n 1 \
    | cut -d= -f2- \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
          -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

is_placeholder() {
  local value="$1" placeholder
  for placeholder in "${PLACEHOLDER_VALUES[@]}"; do
    [ "$value" = "$placeholder" ] && return 0
  done
  return 1
}

# Informa del estado de una variable de `.env` sin revelar su contenido.
report_var() {
  local key="$1" desc="$2" value
  value="$(env_value "$key")"
  if is_placeholder "$value"; then
    pending "${key} -- ${desc}"
    PENDING_VARS=$((PENDING_VARS + 1))
  else
    ok "${key} -- ${desc}"
  fi
}

check_compose() {
  if has_cmd docker && docker compose version >/dev/null 2>&1; then
    ok "docker compose (plugin)"
  elif has_cmd docker-compose; then
    ok "docker-compose (binario)"
  else
    miss "docker compose / docker-compose"
    MISSING_REQUIRED=$((MISSING_REQUIRED + 1))
  fi
}

check_twg() {
  if has_cmd twg; then
    ok "twg en el PATH ($(command -v twg))"
  elif [ -x "${HOME}/.local/bin/twg" ]; then
    ok "twg en \${HOME}/.local/bin/twg"
  else
    warn "twg no encontrado en el PATH ni en \${HOME}/.local/bin/twg"
  fi

  if [ -x "$TWG_IN_BIN" ]; then
    ok "bin/twg presente (el contenedor lo usara como adaptador)"
  else
    warn "bin/twg no existe: el contenedor arrancara sin twg y no podra leer el diff"
    printf '              dejalo con: cp ~/.local/bin/twg %s\n' "bin/twg"
    printf '              detalles en bin/README.md\n'
  fi

  # La credencial de twg viaja por entorno (TWG_BBC_TOKEN), no como fichero: el
  # contenedor no monta el ~/.config/twg del host. Sin ella twg arranca pero no
  # puede hablar con Bitbucket/Jira; mejor avisar aqui que en la primera review.
  local twg_token
  twg_token="$(env_value TWG_BBC_TOKEN)"
  if [ -n "$twg_token" ] && [ "$twg_token" != "change-me" ]; then
    ok "credencial de twg presente en .env (TWG_BBC_TOKEN)"
  else
    warn "TWG_BBC_TOKEN vacio en .env: el worker no podra leer el diff"
    printf '              crea un API token scoped de un actor de servicio y ponlo en .env\n'
    printf '              (ver .env.example)\n'
  fi
}

printf '\n== bootstrap-env: arranque local de reviews ==\n\n'

printf -- '-- .env --\n'
if [ -f "$ENV_FILE" ]; then
  warn ".env ya existe; no se sobreescribe (nada que hacer)"
elif [ ! -f "$ENV_EXAMPLE" ]; then
  miss ".env.example no encontrado en ${REPO_ROOT}"
  MISSING_REQUIRED=$((MISSING_REQUIRED + 1))
else
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  ok "creado .env a partir de .env.example"
fi
# Los secretos no deben ser legibles por otros usuarios del host.
if [ -f "$ENV_FILE" ]; then
  if chmod 600 "$ENV_FILE" 2>/dev/null; then
    ok ".env solo legible por su dueno (chmod 600)"
  else
    warn "no se pudieron restringir los permisos de .env (chmod 600)"
  fi
fi

printf -- '\n-- dependencias --\n'
if has_cmd docker; then ok "docker"; else miss "docker"; MISSING_REQUIRED=$((MISSING_REQUIRED + 1)); fi
check_compose
if has_cmd uv; then ok "uv"; else miss "uv"; MISSING_REQUIRED=$((MISSING_REQUIRED + 1)); fi
# twg solo hace falta como adaptador de Bitbucket; con GitHub el worker no lo usa.
if [ "$(env_value VCS_PROVIDER)" = "github" ]; then
  printf '  [info]      VCS_PROVIDER=github: no se comprueba twg (no se usa)\n'
else
  check_twg
fi

printf -- '\n-- variables a rellenar en .env --\n'
report_var TOOLING_WEBHOOK_SECRET "firma HMAC del POST del CI (POST /webhook/tooling)"
report_var TEAMS_WEBHOOK_URL      "Incoming Webhook de Teams (teaser de la review)"
# El proveedor elige que identidad y credencial se exigen. twg (Bitbucket) es el
# defecto; github requiere owner/repo/token de la API.
if [ "$(env_value VCS_PROVIDER)" = "github" ]; then
  report_var GITHUB_OWNER "owner del repo a revisar (p. ej. cqm)"
  report_var GITHUB_REPO  "repo del PR a revisar (p. ej. emotivoo-v2)"
  report_var GITHUB_TOKEN "PAT fine-grained (pull_requests: write + contents: read)"
else
  report_var BITBUCKET_WORKSPACE "workspace de Bitbucket (p. ej. cqm)"
  report_var BITBUCKET_REPO      "repo del PR a revisar (p. ej. emotivoo-v2)"
fi

if [ "$(env_value LLM_REVIEWER_ENABLED)" = "true" ]; then
  printf '  (persona LLM habilitada: LLM_REVIEWER_ENABLED=true)\n'
  report_var LLM_MODEL   "modelo del proveedor compatible OpenAI"
  report_var LLM_API_KEY "clave del proveedor LLM"
else
  printf '  (LLM opcional: se activa con LLM_REVIEWER_ENABLED=true)\n'
fi

printf -- '\n-- resumen --\n'
if [ "$MISSING_REQUIRED" -gt 0 ]; then
  miss "${MISSING_REQUIRED} dependencia(s) obligatoria(s) sin resolver"
fi
if [ "$PENDING_VARS" -gt 0 ]; then
  warn "${PENDING_VARS} variable(s) de .env aun con placeholder o vacias"
fi
if [ "$MISSING_REQUIRED" -eq 0 ] && [ "$PENDING_VARS" -eq 0 ]; then
  ok "entorno listo: docker compose up --build"
fi

[ "$MISSING_REQUIRED" -eq 0 ] || exit 1
