#!/bin/sh
# Lee el master key generado al levantar el stack. Si NAN_API_KEY está
# vacía, usa LLM_API_KEY: es la misma clave del clúster, y este proceso
# es el único que la recibe con ese fin.
set -eu
if [ -z "${LITELLM_MASTER_KEY:-}" ]; then
  if [ ! -s /run/gateway/token ]; then
    echo "falta el token del gateway" >&2
    exit 1
  fi
  LITELLM_MASTER_KEY=$(tr -d '\n' < /run/gateway/token)
  export LITELLM_MASTER_KEY
fi
if [ -z "${NAN_API_KEY:-}" ] && [ -n "${LLM_API_KEY:-}" ]; then
  export NAN_API_KEY="$LLM_API_KEY"
fi
exec docker/prod_entrypoint.sh "$@"
