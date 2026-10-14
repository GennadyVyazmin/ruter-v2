#!/usr/bin/env bash
set -Eeuo pipefail

ENV_FILE=/etc/ruter-v2/network.env
STATE=/var/lib/ruter-v2/route.state
TABLE=220
SELF_PREF=12200
LAN_PREF=12210
TUN=ruter-v2-tun

[[ $EUID -eq 0 ]] || { echo 'Run as root' >&2; exit 1; }
[[ -f $ENV_FILE ]] || { echo 'Missing network configuration' >&2; exit 1; }
# network.env is written by Ruter v2, root-owned and mode 0600.
source "$ENV_FILE"

owned_rule() { ip -4 rule show | grep -Eq "^$1:"; }
clean_reserved() {
  ! owned_rule "$SELF_PREF" && ! owned_rule "$LAN_PREF" &&
  [[ -z $(ip -4 route show table "$TABLE" 2>/dev/null) ]]
}

start() {
  [[ ! -e $STATE ]] || { echo 'Ruter v2 route already active' >&2; exit 1; }
  clean_reserved || { echo 'Routing table/priorities already occupied; refusing to change them' >&2; exit 1; }
  for _ in {1..50}; do
    if ip link show dev "$TUN" >/dev/null 2>&1; then break; fi
    sleep 0.2
  done
  ip link show dev "$TUN" >/dev/null
  ip -4 addr show dev "$LAN_IF" | grep -Fq "inet $LAN_IP/"
  ip -4 route show default | grep -Fq " dev $LAN_IF "
  original_forward=$(< /proc/sys/net/ipv4/ip_forward)
  rollback() {
    ip -4 rule del pref "$LAN_PREF" 2>/dev/null || true
    ip -4 rule del pref "$SELF_PREF" 2>/dev/null || true
    ip -4 route flush table "$TABLE" 2>/dev/null || true
    sysctl -qw "net.ipv4.ip_forward=$original_forward" || true
  }
  trap rollback ERR
  ip -4 route add "$LAN_CIDR" dev "$LAN_IF" scope link table "$TABLE"
  ip -4 route add default dev "$TUN" table "$TABLE"
  ip -4 rule add pref "$SELF_PREF" from "$LAN_IP/32" lookup main
  ip -4 rule add pref "$LAN_PREF" from "$LAN_CIDR" lookup "$TABLE"
  sysctl -qw net.ipv4.ip_forward=1
  printf '%s\n' "$original_forward" > "$STATE"
  chmod 600 "$STATE"
  trap - ERR
}

stop() {
  [[ -f $STATE ]] || return 0
  original_forward=$(< "$STATE")
  # Remove only the rules reserved by this instance, and only if they match.
  if ip -4 rule show | grep -Fq "$LAN_PREF: from $LAN_CIDR lookup $TABLE"; then
    ip -4 rule del pref "$LAN_PREF" from "$LAN_CIDR" lookup "$TABLE"
  fi
  if ip -4 rule show | grep -Fq "$SELF_PREF: from $LAN_IP lookup main"; then
    ip -4 rule del pref "$SELF_PREF" from "$LAN_IP/32" lookup main
  fi
  ip -4 route flush table "$TABLE"
  if [[ $original_forward == 0 && $(< /proc/sys/net/ipv4/ip_forward) == 1 ]]; then
    sysctl -qw net.ipv4.ip_forward=0
  fi
  rm -f -- "$STATE"
}

case ${1:-} in
  start) start ;;
  stop) stop ;;
  *) echo 'Usage: route.sh start|stop' >&2; exit 2 ;;
esac
