#!/usr/bin/env bash
# Configure a cloned jumpbox server with hostname, static IP, and DNS.
#
# Reads defaults from run_config.yml when available. All values can be
# overridden via environment variables.
#
# Usage:
#   ./scripts/configure_sap_clone.sh <hostname-short> <ip>
#
# Examples:
#   ./scripts/configure_sap_clone.sh server1 10.x.x.4
#   GATEWAY=10.x.x.1 DNS=10.x.x.10 ./scripts/configure_sap_clone.sh server1 10.x.x.4
#
# Environment overrides:
#   IFACE         NIC to configure (default: auto-detect from ens1f0np0, ens4f0np0)
#   OLD_HOST      old hostname to remove from /etc/hosts (default: auto-detect)
#   GATEWAY       default gateway IP
#   DNS           comma-separated DNS servers
#   DNS_SEARCH    DNS search domain
#   CIDR_MASK     CIDR mask (default: /22)
#   JUMPBOX_IP    jumpbox IP for connectivity verification
#   DOMAIN        domain suffix for FQDN

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
CONFIG="${REPO_ROOT}/run_config.yml"

HOST_SHORT="${1:?usage: $0 <hostname-short> <ip>   e.g. server1 10.x.x.4}"
IP="${2:?usage: $0 <hostname-short> <ip>}"

# Read defaults from run_config.yml if available
_cfg() {
    local key="$1"
    if [[ -f "$CONFIG" ]]; then
        grep "^${key}:" "$CONFIG" 2>/dev/null | head -1 | awk '{print $2}' | tr -d '"' || true
    fi
}

_cfg_lab() {
    local key="$1"
    if [[ -f "$CONFIG" ]]; then
        grep "  ${key}:" "$CONFIG" 2>/dev/null | head -1 | awk '{print $2}' | tr -d '"' || true
    fi
}

DOMAIN="${DOMAIN:-$(_cfg domain)}"
GATEWAY="${GATEWAY:-$(_cfg_lab router)}"
DNS="${DNS:-$(_cfg_lab dns_server)}"
DNS_SEARCH="${DNS_SEARCH:-${DOMAIN}}"
CIDR_MASK="${CIDR_MASK:-$(_cfg_lab cidr_mask)}"
CIDR_MASK="${CIDR_MASK:-/22}"
JUMPBOX_IP="${JUMPBOX_IP:-}"

# Validate required values
_require() {
    local name="$1" val="$2"
    if [[ -z "$val" ]]; then
        echo "ERROR: ${name} is not set."
        echo "Set it via environment variable or add it to run_config.yml"
        echo ""
        echo "Example: ${name}=<value> $0 ${HOST_SHORT} ${IP}"
        exit 1
    fi
}

_require "DOMAIN" "$DOMAIN"
_require "GATEWAY" "$GATEWAY"
_require "DNS" "$DNS"

FQDN="${HOST_SHORT}.${DOMAIN}"
CIDR="${IP}${CIDR_MASK}"
PRIMARY_IFACE="${IFACE:-ens1f0np0}"
FALLBACK_IFACE="ens4f0np0"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

require_root() {
    [[ "$(id -u)" -eq 0 ]] || die "run as root"
}

preflight() {
    require_root
    command -v nmcli >/dev/null || die "nmcli not found"
    command -v hostnamectl >/dev/null || die "hostnamectl not found"
    if ! rpm -q openssh-server >/dev/null 2>&1; then
        die "openssh-server not installed"
    fi

    log "Configuration:"
    log "  Hostname: ${FQDN}"
    log "  IP: ${CIDR}"
    log "  Gateway: ${GATEWAY}"
    log "  DNS: ${DNS}"
    log "  Search: ${DNS_SEARCH}"
    log "  Primary NIC: ${PRIMARY_IFACE}"
}

guess_old_host() {
    if [[ -n "${OLD_HOST:-}" ]]; then
        return
    fi
    OLD_HOST="$(hostname -s 2>/dev/null || true)"
    if [[ "$OLD_HOST" == "$HOST_SHORT" ]]; then
        OLD_HOST=""
    fi
}

disable_profile() {
    local name="$1"
    nmcli con down "$name" 2>/dev/null || true
    nmcli con mod "$name" ipv4.method disabled 2>/dev/null || true
}

disable_other_sap_ports() {
    local active_iface="$1"
    local name
    while read -r name; do
        [[ -z "$name" || "$name" == "lo" ]] && continue
        local dev
        dev="$(nmcli -t -f GENERAL.DEVICE con show "$name" 2>/dev/null | cut -d: -f2-)"
        [[ "$dev" == "$active_iface" || -z "$dev" || "$dev" == "--" ]] && continue
        case "$dev" in
            ens1f0np0|ens4f0np0) disable_profile "$name" ;;
        esac
    done < <(nmcli -t -f NAME con show)
}

configure_iface() {
    local iface="$1"
    local con_name

    log "configuring ${iface} with ${CIDR}"

    if ip link show "$iface" >/dev/null 2>&1; then
        :
    else
        return 1
    fi

    con_name="$(nmcli -t -f GENERAL.CONNECTION device show "$iface" 2>/dev/null | cut -d: -f2-)"
    if [[ -z "$con_name" || "$con_name" == "--" ]]; then
        con_name="$iface"
    fi

    if nmcli con show "$con_name" >/dev/null 2>&1; then
        local method
        method="$(nmcli -g ipv4.method con show "$con_name" 2>/dev/null || true)"
        if [[ "$method" == "disabled" ]]; then
            log "profile ${con_name} has ipv4.method=disabled; recreating"
            nmcli con del "$con_name"
            con_name="$iface"
        fi
    fi

    if nmcli con show "$con_name" >/dev/null 2>&1; then
        nmcli con mod "$con_name" \
            connection.interface-name "$iface" \
            ipv4.method manual \
            ipv4.addresses "$CIDR" \
            ipv4.gateway "$GATEWAY" \
            ipv4.dns "$DNS" \
            ipv4.dns-search "$DNS_SEARCH"
    else
        nmcli con add con-name "$con_name" type ethernet ifname "$iface" \
            ipv4.method manual \
            ipv4.addresses "$CIDR" \
            ipv4.gateway "$GATEWAY" \
            ipv4.dns "$DNS" \
            ipv4.dns-search "$DNS_SEARCH"
    fi

    nmcli con up "$con_name"
    disable_other_sap_ports "$iface"

    ip -4 addr show dev "$iface" | grep -q "$IP" || return 1
    ping -c 1 -W 2 "$GATEWAY" >/dev/null 2>&1 || return 1
    return 0
}

set_hostname() {
    log "setting hostname to ${FQDN}"
    hostnamectl set-hostname "$FQDN"
    if [[ -n "${OLD_HOST:-}" ]]; then
        sed -i "/${OLD_HOST}/d" /etc/hosts
    fi
    if ! grep -q "$HOST_SHORT" /etc/hosts; then
        echo "${IP}   ${FQDN} ${HOST_SHORT}" >> /etc/hosts
    fi
}

verify() {
    log "verification"
    hostname -f
    ip -4 route show default || true
    ip -4 addr show dev "$ACTIVE_IFACE"
    ping -c 2 "$GATEWAY"
    if [[ -n "${JUMPBOX_IP:-}" ]]; then
        ping -c 2 "$JUMPBOX_IP"
    fi
    systemctl is-active sshd
    echo
    echo "From jumpbox:"
    echo "  ping -c 2 ${IP}"
    echo "  ssh root@${IP}"
    echo
    echo "DCI inventory MAC (active NIC):"
    ip link show "$ACTIVE_IFACE" | awk '/link\/ether/ {print $2}'
}

ACTIVE_IFACE=""
preflight
guess_old_host
set_hostname

if configure_iface "$PRIMARY_IFACE"; then
    ACTIVE_IFACE="$PRIMARY_IFACE"
elif [[ -n "${IFACE:-}" ]]; then
    die "forced interface ${IFACE} failed"
else
    log "${PRIMARY_IFACE} failed; trying ${FALLBACK_IFACE}"
    if configure_iface "$FALLBACK_IFACE"; then
        ACTIVE_IFACE="$FALLBACK_IFACE"
    else
        die "could not bring up ${PRIMARY_IFACE} or ${FALLBACK_IFACE}"
    fi
fi

verify
