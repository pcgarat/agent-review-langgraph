#!/bin/sh
# Crea el master key del gateway si el volumen aún no lo tiene.
# No es la clave del clúster. Reiniciar no lo rota: hay que borrar el volumen.
set -eu
# La variable solo existe para probar el script fuera del contenedor.
file=${GATEWAY_TOKEN_FILE:-/run/gateway/token}
mkdir -p "$(dirname "$file")"
if [ -s "$file" ]; then
  exit 0
fi
token=$(head -c 32 /dev/urandom | base64 | tr -d '\n')
# Lo leen root en claude-runner y en nan-gateway. El hijo del CLI no es root.
umask 077
printf 'sk-%s\n' "$token" > "$file"
chmod 600 "$file"
