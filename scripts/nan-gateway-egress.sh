#!/bin/sh
# Cierra la salida del netns de nan-gateway. Este proceso es el único con
# NET_ADMIN: LiteLLM no puede borrar las reglas. Solo sale HTTPS hacia
# api.nan.builders; el resto (telemetría, otro host) se cae.
set -eu

HOST=api.nan.builders
# Rutas de Compose. Las variables solo existen para probar el script fuera
# del contenedor.
DIR=${NAN_EGRESS_DIR:-/run/egress}
READY=$DIR/ready
RESOLV=${NAN_EGRESS_RESOLV:-/etc/resolv.conf}
INTERVAL=60

if ! command -v iptables >/dev/null 2>&1; then
  echo "falta iptables" >&2
  exit 1
fi

mkdir -p "$DIR"
# La marca de un arranque anterior vive en el volumen. Se borra antes de
# tocar nada: el gateway no puede tomarla por la de este netns.
rm -f "$READY"

allow_dns() {
  # El resolver es el de este contenedor, no el de LiteLLM: no comparten
  # mounts. En Compose suele ser 127.0.0.11; aquí se lee, no se adivina.
  ns=$(awk '/^nameserver[ \t]/ { print $2 }' "$RESOLV")
  if [ -z "$ns" ]; then
    echo "resolv.conf no trae nameserver" >&2
    return 1
  fi
  for ip in $ns; do
    case $ip in
      *:*)
        if [ -e /proc/net/if_inet6 ]; then
          ip6tables -A OUTPUT -p udp -d "$ip" --dport 53 -j ACCEPT
          ip6tables -A OUTPUT -p tcp -d "$ip" --dport 53 -j ACCEPT
        fi
        ;;
      *)
        iptables -A OUTPUT -p udp -d "$ip" --dport 53 -j ACCEPT
        iptables -A OUTPUT -p tcp -d "$ip" --dport 53 -j ACCEPT
        ;;
    esac
  done
}

lock_base() {
  iptables -F OUTPUT
  iptables -P OUTPUT DROP
  iptables -A OUTPUT -o lo -j ACCEPT
  iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

  if [ -e /proc/net/if_inet6 ]; then
    ip6tables -F OUTPUT
    ip6tables -P OUTPUT DROP
    ip6tables -A OUTPUT -o lo -j ACCEPT
    ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  fi
  allow_dns
  # Dos cadenas: el refresco llena la que no se usa y cambia el salto de la
  # regla 1 de una vez, sin un instante con la cadena vacía.
  for chain in NAN_EGRESS_A NAN_EGRESS_B; do
    iptables -N "$chain" 2>/dev/null || iptables -F "$chain"
  done
  iptables -I OUTPUT 1 -j NAN_EGRESS_A
  if [ -e /proc/net/if_inet6 ]; then
    for chain in NAN_EGRESS_A NAN_EGRESS_B; do
      ip6tables -N "$chain" 2>/dev/null || ip6tables -F "$chain"
    done
    ip6tables -I OUTPUT 1 -j NAN_EGRESS_A
  fi
}

ACTIVE=NAN_EGRESS_A

allow_host() {
  v4=$(getent ahostsv4 "$HOST" 2>/dev/null | awk '{print $1}' | sort -u)
  if [ -z "$v4" ]; then
    echo "no se resolvio $HOST" >&2
    return 1
  fi
  if [ "$ACTIVE" = NAN_EGRESS_A ]; then next=NAN_EGRESS_B; else next=NAN_EGRESS_A; fi
  # Se llena la cadena en reserva y luego se cambia el salto: un refresco no
  # deja IPs viejas ni corta las conexiones nuevas mientras tanto.
  iptables -F "$next"
  for ip in $v4; do
    iptables -A "$next" -p tcp -d "$ip" --dport 443 -j ACCEPT
  done
  iptables -R OUTPUT 1 -j "$next"
  iptables -F "$ACTIVE"
  if [ -e /proc/net/if_inet6 ]; then
    v6=$(getent ahostsv6 "$HOST" 2>/dev/null | awk '{print $1}' | sort -u)
    ip6tables -F "$next"
    for ip in $v6; do
      ip6tables -A "$next" -p tcp -d "$ip" --dport 443 -j ACCEPT
    done
    ip6tables -R OUTPUT 1 -j "$next"
    ip6tables -F "$ACTIVE"
  fi
  ACTIVE=$next
}

netns_alive() {
  # Si el gateway se reinicia, su netns nuevo no tiene estas reglas y este
  # proceso se queda en el viejo, ya sin interfaces. Salir deja que
  # `restart` lo vuelva a unir al netns vivo.
  awk 'NR > 2 { sub(":", "", $1); if ($1 != "lo") n++ } END { exit n == 0 }' /proc/net/dev
}

lock_base
# Sin DNS al arrancar no se abre nada: se reintenta y el gateway sigue
# esperando la marca.
until allow_host; do
  sleep 5
done
# La marca nombra este netns. El gateway solo arranca si es también el suyo.
readlink /proc/self/ns/net > "$READY"
chmod 644 "$READY"

while true; do
  sleep "$INTERVAL"
  if ! netns_alive; then
    echo "el netns del gateway ya no existe" >&2
    rm -f "$READY"
    exit 1
  fi
  allow_host || echo "no se pudo refrescar $HOST" >&2
done
