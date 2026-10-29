#!/bin/sh
# Cierra la salida del netns de nan-gateway. Este proceso es el único con
# NET_ADMIN: LiteLLM no puede borrar las reglas. Solo sale HTTPS hacia
# api.nan.builders; el resto (telemetría, otro host) se cae.
set -eu

HOST=api.nan.builders
READY=/run/egress/ready
INTERVAL=60

if ! command -v iptables >/dev/null 2>&1; then
  echo "falta iptables" >&2
  exit 1
fi

mkdir -p /run/egress

allow_dns() {
  # El resolver es el de este contenedor, no el de LiteLLM: no comparten
  # mounts. En Compose suele ser 127.0.0.11; aquí se lee, no se adivina.
  ns=$(awk '/^nameserver[ \t]/ { print $2 }' /etc/resolv.conf)
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
  iptables -N NAN_EGRESS 2>/dev/null || iptables -F NAN_EGRESS
  iptables -A OUTPUT -j NAN_EGRESS
  if [ -e /proc/net/if_inet6 ]; then
    ip6tables -N NAN_EGRESS 2>/dev/null || ip6tables -F NAN_EGRESS
    ip6tables -A OUTPUT -j NAN_EGRESS
  fi
}

allow_host() {
  v4=$(getent ahostsv4 "$HOST" 2>/dev/null | awk '{print $1}' | sort -u)
  if [ -z "$v4" ]; then
    echo "no se resolvio $HOST" >&2
    return 1
  fi
  # La cadena se vacía y se rellena: un refresco no deja IPs viejas.
  iptables -F NAN_EGRESS
  for ip in $v4; do
    iptables -A NAN_EGRESS -p tcp -d "$ip" --dport 443 -j ACCEPT
  done
  if [ ! -e /proc/net/if_inet6 ]; then
    return 0
  fi
  v6=$(getent ahostsv6 "$HOST" 2>/dev/null | awk '{print $1}' | sort -u)
  ip6tables -F NAN_EGRESS
  for ip in $v6; do
    ip6tables -A NAN_EGRESS -p tcp -d "$ip" --dport 443 -j ACCEPT
  done
}

lock_base
allow_host
printf 'ok\n' > "$READY"
chmod 644 "$READY"

while true; do
  sleep "$INTERVAL"
  allow_host || echo "no se pudo refrescar $HOST" >&2
done
