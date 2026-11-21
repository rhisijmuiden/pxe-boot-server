#!/usr/bin/env bash
# Creates a PXE Boot Server container on a Proxmox VE host in one go: downloads the LXC template
# (when it is not in the template storage yet), creates and starts the container, and shows the
# web address with the generated username and password.
#
#   Run as root on the Proxmox host:
#   bash <(curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/pxe-boot-server/main/lxc/proxmox-create-ct.sh)
#
# Options:
#   --ctid N              container id (default: next free id)
#   --hostname NAME       default: pxe-boot-server
#   --storage NAME        storage for the root disk (default: local-lvm)
#   --disk GB             root disk size (default: 100; unpacked ISOs need room)
#   --template-storage S  storage with CT templates (default: local)
#   --bridge NAME         default: vmbr0 (must be on the network the clients boot from)
#   --vlan TAG            VLAN tag for the network card
#   --ip CIDR|dhcp        default: dhcp
#   --gw ADDRESS          gateway when --ip is a static address
#   --memory MB           default: 2048
#   --cores N             default: 2
#   --refresh             download the template again (newest version)
set -euo pipefail

URL="https://github.com/rhisijmuiden/pxe-boot-server/releases/latest/download/pxe-boot-server-debian-13-amd64.tar.zst"
FILE=pxe-boot-server-debian-13-amd64.tar.zst
CTID=""; HOST=pxe-boot-server; STORAGE=local-lvm; DISK=100; TSTORE=local; BRIDGE=vmbr0; VLAN=""
IP=dhcp; GW=""; MEM=2048; CORES=2; REFRESH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ctid) CTID="$2"; shift 2 ;;
    --hostname) HOST="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --disk) DISK="$2"; shift 2 ;;
    --template-storage) TSTORE="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --vlan) VLAN="$2"; shift 2 ;;
    --ip) IP="$2"; shift 2 ;;
    --gw) GW="$2"; shift 2 ;;
    --memory) MEM="$2"; shift 2 ;;
    --cores) CORES="$2"; shift 2 ;;
    --refresh) REFRESH=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done
say() { echo "==> $*"; }
command -v pct >/dev/null || { echo "run this on a Proxmox VE host" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

TPL="$TSTORE:vztmpl/$FILE"
DEST="$(pvesm path "$TPL")"
if [ ! -s "$DEST" ] || [ "$REFRESH" = 1 ]; then
  say "Downloading the template to $TSTORE"
  curl -fL --retry 3 -o "$DEST.part" "$URL"
  curl -fsSL "$URL.sha256" | awk '{print $1}' > "$DEST.sha256"
  [ "$(sha256sum "$DEST.part" | awk '{print $1}')" = "$(cat "$DEST.sha256")" ] || { rm -f "$DEST.part" "$DEST.sha256"; echo "checksum mismatch" >&2; exit 1; }
  mv "$DEST.part" "$DEST"; rm -f "$DEST.sha256"
fi

[ -n "$CTID" ] || CTID="$(pvesh get /cluster/nextid)"
NET="name=eth0,bridge=$BRIDGE,ip=$IP"
[ -n "$GW" ] && NET="$NET,gw=$GW"
[ -n "$VLAN" ] && NET="$NET,tag=$VLAN"
say "Creating container $CTID ($HOST)"
pct create "$CTID" "$TPL" --hostname "$HOST" --ostype debian --unprivileged 1 --features nesting=1 \
  --cores "$CORES" --memory "$MEM" --swap 512 --rootfs "$STORAGE:$DISK" --net0 "$NET" \
  --onboot 1 --description "PXE Boot Server" >/dev/null
pct start "$CTID"

say "Waiting for the first boot"
for _ in $(seq 1 180); do
  pct exec "$CTID" -- test -e /etc/pxe-boot-server/firstboot 2>/dev/null || \
    { pct exec "$CTID" -- systemctl is-active -q pxe-boot-server-firstboot 2>/dev/null && break; }
  sleep 2
done
CT_IP="$(pct exec "$CTID" -- sh -c "ip -o -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if(\$i==\"src\") print \$(i+1)}'" || true)"
USER_="$(pct exec "$CTID" -- sed -n 's/^username=//p' /etc/pxe-boot-server/initial-admin 2>/dev/null || true)"
PASS="$(pct exec "$CTID" -- sed -n 's/^password=//p' /etc/pxe-boot-server/initial-admin 2>/dev/null || true)"
echo
echo "  URL:      http://${CT_IP:-<container-ip>}:80/"
echo "  Username: ${USER_:-<see the container console>}"
echo "  Password: ${PASS:-<see the container console>}"
echo
