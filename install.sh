#!/usr/bin/env bash
# PXE Boot Server installer for Debian 13 (trixie).
#
#   curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/pxe-boot-server/main/install.sh | sudo bash
#
# Options (after "bash -s --" when piping):
#   --channel main|test   branch to install (default: main)
#   --interface NAME      network interface for PXE (default: the one with the default route)
#   --force               install on something other than Debian 13
set -euo pipefail
export LC_ALL=C DEBIAN_FRONTEND=noninteractive

# Everything sits in main() so "curl | bash" reads the whole script before running it.
main() {

REPO_URL="${PXE_REPO:-https://github.com/rhisijmuiden/pxe-boot-server.git}"
DEST=/opt/pxe-boot-server
CHANNEL=main
IFACE=""
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --channel) CHANNEL="${2:?}"; shift 2 ;;
    --interface) IFACE="${2:?}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,10p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done
[[ "$CHANNEL" == main || "$CHANNEL" == test ]] || { echo "channel must be main or test" >&2; exit 1; }

c_red=$'\e[31m'; c_grn=$'\e[32m'; c_cyn=$'\e[36m'; c_off=$'\e[0m'
say() { echo "${c_cyn}==>${c_off} $*"; }
fail() { echo "${c_red}error:${c_off} $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || fail "run as root (sudo bash install.sh)"
. /etc/os-release
if [ "${ID:-}" != debian ] || [ "${VERSION_ID:-}" != 13 ]; then
  [ "$FORCE" = 1 ] || fail "this installer is made for Debian 13 (found: ${PRETTY_NAME:-unknown}). Use --force to try anyway."
fi

say "Installing packages"
# Placeholder dnsmasq config before the package starts it: no DNS, no DHCP until configured.
mkdir -p /etc/dnsmasq.d
[ -f /etc/dnsmasq.d/pxe-boot-server.conf ] || printf '# Placeholder until PXE Boot Server applies its configuration\nport=0\n' > /etc/dnsmasq.d/pxe-boot-server.conf
apt-get -o DPkg::Lock::Timeout=600 update -q
apt-get -o DPkg::Lock::Timeout=600 install -y -q --no-install-recommends \
  ca-certificates curl git sudo iproute2 openssl \
  python3 python3-venv python3-pip \
  nginx dnsmasq samba samba-common-bin \
  xorriso syslinux-common dhcpcd-base
# Debian's dnsmasq package can also read /etc/default/dnsmasq; make sure it uses /etc/dnsmasq.d.
if [ -f /etc/default/dnsmasq ] && grep -q '^CONFIG_DIR=' /etc/default/dnsmasq; then
  grep -q 'dnsmasq.d' /etc/default/dnsmasq || echo 'CONFIG_DIR=/etc/dnsmasq.d,.dpkg-dist,.dpkg-old,.dpkg-new' >> /etc/default/dnsmasq
fi

say "Users"
id pxeboot >/dev/null 2>&1 || useradd --system --home-dir /var/lib/pxe-boot-server --shell /usr/sbin/nologin pxeboot
usermod -aG adm pxeboot   # read the nginx access log for live progress
id pxe >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin pxe

say "Getting the code ($CHANNEL)"
git config --system --get-all safe.directory 2>/dev/null | grep -qx "$DEST" || git config --system --add safe.directory "$DEST"
if [ -d "$DEST/.git" ]; then
  git -C "$DEST" fetch -q origin "+refs/heads/$CHANNEL:refs/remotes/origin/$CHANNEL"
  if [ -n "$(git -C "$DEST" status --porcelain 2>/dev/null)" ]; then
    # Keep a copy of local code edits, then continue with a clean checkout.
    BK="/var/lib/pxe-boot-server/update-backups/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$BK"
    git -C "$DEST" diff HEAD > "$BK/local-changes.patch" || true
    git -C "$DEST" status --porcelain > "$BK/files.txt" || true
    say "Local changes in $DEST saved to $BK and discarded"
    git -C "$DEST" reset -q --hard HEAD
    git -C "$DEST" clean -q -f -d
  fi
  git -C "$DEST" checkout -q -f -B "$CHANNEL" "origin/$CHANNEL"
  git -C "$DEST" reset -q --hard "origin/$CHANNEL"
else
  git clone -q --branch "$CHANNEL" "$REPO_URL" "$DEST"
fi

say "Configuring the system"
bash "$DEST/deploy/post-update.sh"

FRESH=0
[ -f /var/lib/pxe-boot-server/app.db ] || FRESH=1

say "Starting services"
systemctl restart pxe-boot-vlans pxe-boot-server
for _ in $(seq 1 30); do
  curl -fs http://127.0.0.1:8080/healthz >/dev/null 2>&1 && break
  sleep 1
done
curl -fs http://127.0.0.1:8080/healthz >/dev/null || fail "the web app did not start; see: journalctl -u pxe-boot-server"

if [ -z "$IFACE" ]; then
  IFACE="$(ip -o route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')"
fi
SMB_PW=""
if [ "$FRESH" = 1 ] || ! pdbedit -L 2>/dev/null | grep -q '^pxe:'; then
  SMB_PW="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)"
  printf '%s\n%s\n' "$SMB_PW" "$SMB_PW" | smbpasswd -s -a pxe >/dev/null
  smbpasswd -e pxe >/dev/null
fi

say "Applying the PXE configuration"
cd "$DEST"
sudo -u pxeboot env PYTHONDONTWRITEBYTECODE=1 CHANNEL="$CHANNEL" IFACE="$IFACE" SMB_PW="$SMB_PW" FRESH="$FRESH" \
  "$DEST/venv/bin/python" - <<'PY'
import os
from app import db, dnsmasq
db.migrate()
if os.environ["SMB_PW"]:
    db.set_setting("smb_password", os.environ["SMB_PW"])
if os.environ["FRESH"] == "1" and os.environ["IFACE"] and not db.get_setting("interfaces"):
    db.set_setting("interfaces", [os.environ["IFACE"]])
if os.environ["FRESH"] == "1":
    db.set_setting("update_channel", os.environ.get("CHANNEL", "main"))
dnsmasq.apply()
PY
systemctl restart pxe-boot-server

# The web interface is reached on the address of the default route, which can differ from the PXE interface.
IP="$(ip -o -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
[ -n "$IP" ] || IP="$(ip -o -4 addr show dev "${IFACE:-lo}" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
echo
echo "${c_grn}PXE Boot Server $(cat "$DEST/VERSION") is installed.${c_off}"
echo "  Web interface:  http://${IP:-<server-ip>}/"
[ "$FRESH" = 1 ] && echo "  Open it to create the first admin account."
echo "  PXE interface:  ${IFACE:-all} (proxy DHCP, change it under Network & PXE)"
echo "  Update channel: $CHANNEL"
}

main "$@"
