#!/bin/sh
# Lee el master key generado al levantar el stack. Si NAN_API_KEY está
# vacía, usa LLM_API_KEY: es la misma clave del clúster, y este proceso
# es el único que la recibe con ese fin.
set -eu
# El sidecar escribe la marca cuando OUTPUT ya está cerrado. Arrancar antes
# dejaría una ventana en la que LiteLLM puede salir a cualquier sitio. La
# marca vive en un volumen: solo vale si nombra el netns de este arranque.
# Las variables solo existen para probar el script fuera del contenedor.
ready=${NAN_EGRESS_DIR:-/run/egress}/ready
tries=${NAN_EGRESS_WAIT_TRIES:-240}
netns=$(readlink /proc/self/ns/net)
i=0
while [ "$(cat "$ready" 2>/dev/null || true)" != "$netns" ]; do
  i=$((i + 1))
  if [ "$i" -gt "$tries" ]; then
    echo "el egress del gateway no cerro a tiempo" >&2
    exit 1
  fi
  sleep 0.5
done
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
# Sin clave el health diría que sí y cada review acabaría en la DLQ. Sin
# arrancar, GET /session dice que no y los jobs esperan parados.
if [ -z "${NAN_API_KEY:-}" ]; then
  echo "falta NAN_API_KEY (o LLM_API_KEY)" >&2
  exit 1
fi
exec docker/prod_entrypoint.sh "$@"
