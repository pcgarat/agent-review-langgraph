#!/bin/sh
# Crea el master key del gateway si el volumen aún no lo tiene.
# No es la clave del clúster. Reiniciar no lo rota: hay que borrar el volumen.
set -eu
file=/run/gateway/token
mkdir -p /run/gateway
if [ -s "$file" ]; then
  exit 0
fi
token=$(head -c 32 /dev/urandom | base64 | tr -d '\n')
printf 'sk-%s\n' "$token" > "$file"
chmod 644 "$file"
