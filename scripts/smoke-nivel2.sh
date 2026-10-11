#!/usr/bin/env bash
# smoke-nivel2.sh - Nivel 2 del smoke de Fase 1 (docs/smoke-fase1-20261011.md §2).
#
# EJECUTA UN SMOKE REAL: levanta el stack, firma y deposita el artefacto de
# PHPStan como lo haria el CI y espera que la review se publique en el PR real de
# Bitbucket. Requiere `twg` autenticado y los secretos reales de `.env`. No
# publica nada por si mismo mas alla del POST del smoke.
#
# Uso:
#   scripts/smoke-nivel2.sh <PR_ID> [PIPELINE_ID]
#   PR_ID=417 PIPELINE_ID=smoke-1 scripts/smoke-nivel2.sh
#
# Variables: PR_ID (obligatoria), PIPELINE_ID (opcional, por defecto con
# timestamp), REVIEWS_URL (por defecto http://localhost:8000 o el valor de
# `.env`) y PHPSTAN_ARTIFACT (ruta al JSON de PHPStan; por defecto el fixture del
# repo). Para que el anclaje inline sea representativo, el artefacto deberia
# corresponder a ficheros/lineas del PR que se revisa.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly REPO_ROOT
readonly ENV_FILE="${REPO_ROOT}/.env"
readonly FIXTURE="${PHPSTAN_ARTIFACT:-${REPO_ROOT}/tests/fixtures/phpstan/analysis.json}"

readonly PR_ID="${1:-${PR_ID:-}}"
readonly PIPELINE_ID="${2:-${PIPELINE_ID:-smoke-$(date +%Y%m%d%H%M%S)}}"

readonly REQUIRED_CMDS=(docker twg jq openssl curl)

info() { printf '== %s\n' "$1"; }
warn() { printf '  [aviso] %s\n' "$1" >&2; }
die()  { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

has_cmd() { command -v "$1" >/dev/null 2>&1; }

# Usa `docker compose` (plugin) o `docker-compose` (binario), lo que exista.
compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  else
    docker-compose "$@"
  fi
}

# Espera acotada a que la API responda en /healthz; no aborta el smoke.
wait_for_api() {
  local attempts="${1:-30}" i
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS --max-time 2 "${REVIEWS_URL}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

[ -n "$PR_ID" ] || die "falta PR_ID (pasalo como argumento o variable de entorno)"

info "comprobando dependencias"
for cmd in "${REQUIRED_CMDS[@]}"; do
  has_cmd "$cmd" || die "dependencia ausente: $cmd"
done
if ! docker compose version >/dev/null 2>&1 && ! has_cmd docker-compose; then
  die "dependencia ausente: docker compose (o docker-compose)"
fi

info "cargando configuracion de .env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
else
  warn ".env no encontrado; se usaran solo las variables ya exportadas en el entorno"
fi
# `REVIEWS_URL` puede venir de `.env`; si no, el default. Se fija despues de
# cargarlo porque el `.env` la declararia como de solo lectura.
readonly REVIEWS_URL="${REVIEWS_URL:-http://localhost:8000}"

printf '\n== smoke Nivel 2: artefacto del CI -> cola -> grafo -> PR + Teams ==\n'
printf '   PR_ID=%s  PIPELINE_ID=%s  REVIEWS_URL=%s\n\n' "$PR_ID" "$PIPELINE_ID" "$REVIEWS_URL"

for key in TOOLING_WEBHOOK_SECRET BITBUCKET_WORKSPACE BITBUCKET_REPO; do
  value="${!key:-}"
  [ -n "$value" ] || die "falta $key (definelo en .env)"
  [ "$value" != "change-me" ] || die "$key sigue en su placeholder por defecto (change-me)"
done

[ -f "$FIXTURE" ] || die "no existe el fixture PHPStan: $FIXTURE"

info "levantando el stack (docker compose up --build -d)"
compose_cmd up --build -d

if wait_for_api 30; then
  printf '  [ok]    %s/healthz responde\n' "$REVIEWS_URL"
else
  warn "la API no respondio a /healthz en 30s; revisa 'docker compose logs api'"
fi

info "inyectando la config de twg en el worker (auth de Bitbucket)"
# El contenedor solo recibe el binario; su autenticacion vive en el host.
# Sin esto, `twg prs diff` falla con 77 y la review va a la DLQ.
if [ -d "${HOME}/.config/twg" ]; then
  compose_cmd exec -T worker mkdir -p /root/.config/twg
  for conf in auth.conf auth_oauth.conf; do
    if [ -f "${HOME}/.config/twg/${conf}" ]; then
      compose_cmd cp "${HOME}/.config/twg/${conf}" worker:/root/.config/twg/"${conf}"
      printf '  [ok]    %s\n' "$conf"
    fi
  done
else
  warn "no hay ~/.config/twg en el host; el worker no podra leer el diff"
fi

info "resolviendo HEAD_SHA del PR $PR_ID con twg"
# El repo destino se pasa explicito: el cwd del script es `reviews`, no el
# checkout de Bitbucket, asi que twg no puede autodetectar el workspace.
HEAD_SHA="$(twg bb prs get "$PR_ID" --repo "${BITBUCKET_WORKSPACE}/${BITBUCKET_REPO}" \
  --output json --output-summary none \
  | jq -r '.source.commit.hash')"
[ -n "$HEAD_SHA" ] && [ "$HEAD_SHA" != "null" ] \
  || die "no se pudo resolver head_sha del PR $PR_ID (¿twg autenticado?)"
printf '  [ok]    HEAD_SHA=%s\n' "$HEAD_SHA"

info "construyendo el BODY firmado y depositando el artefacto"
PAYLOAD="$(jq -c . "$FIXTURE")"

BODY="$(jq -n \
  --arg repo "${BITBUCKET_WORKSPACE}/${BITBUCKET_REPO}" \
  --argjson pr_id "$PR_ID" \
  --arg head_sha "$HEAD_SHA" \
  --arg pipeline_id "$PIPELINE_ID" \
  --arg payload "$PAYLOAD" \
  '{repo: $repo, pr_id: $pr_id, head_sha: $head_sha, pipeline_id: $pipeline_id,
    tool: "phpstan", payload: $payload}')"

SIG="$(printf '%s' "$BODY" \
  | openssl dgst -sha256 -hmac "$TOOLING_WEBHOOK_SECRET" -r | cut -d' ' -f1)"

RESPONSE="$(curl -sS -X POST "${REVIEWS_URL}/webhook/tooling" \
  -H "Content-Type: application/json" \
  -H "X-Reviews-Signature: $SIG" \
  --data-binary "$BODY")"

printf '  respuesta: %s\n' "$RESPONSE"

printf '\n== siguiente: verificar el resultado ==\n'
cat <<EOF

  logs del worker:
    docker compose logs -f worker        # busca 'Review completada'

  en el PR $PR_ID de ${BITBUCKET_WORKSPACE}/${BITBUCKET_REPO}:
    - inline:  un comentario por hallazgo, anclado a su linea del diff.
    - resumen: un unico comentario general con conteo por severidad e indice.
    - Teams:   una sola tarjeta teaser (no una por hallazgo).

  criterio de corte: los inline no superan max_inline_comments (20).

  parar el stack:
    docker compose down

EOF
