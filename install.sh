#!/usr/bin/env bash
# PXE Boot Server installer for a bare Debian 13 (trixie): physical, VM or LXC container.
#
#   As root:  curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/pxe-boot-server/main/install.sh | bash
#   No curl:  wget -qO- https://raw.githubusercontent.com/rhisijmuiden/pxe-boot-server/main/install.sh | bash
#
# At the end it shows only the web address, the username and a generated password; everything
# else is configured in the web interface.
#
# Options (after "bash -s --" when piping):
#   --channel main|test   branch to install (default: main)
#   --interface NAME      network interface for PXE (default: the one with the default route)
#   --force               install on something other than Debian 13
#   --image               build a Proxmox LXC template (chroot): no services are started,
#                         the account is created at the container's first boot
set -euo pipefail
export LC_ALL=C DEBIAN_FRONTEND=noninteractive

# Everything sits in main() so "curl | bash" reads the whole script before running it.
main() {

REPO_URL="${PXE_REPO:-https://github.com/rhisijmuiden/pxe-boot-server.git}"
DEST=/opt/pxe-boot-server
CHANNEL=main
IFACE=""
FORCE=0
IMAGE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --channel) CHANNEL="${2:?}"; shift 2 ;;
    --interface) IFACE="${2:?}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --image) IMAGE=1; shift ;;
    -h|--help) sed -n '2,16p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done
[[ "$CHANNEL" == main || "$CHANNEL" == test ]] || { echo "channel must be main or test" >&2; exit 1; }

c_red=$'\e[31m'; c_cyn=$'\e[36m'; c_off=$'\e[0m'
say() { echo "${c_cyn}==>${c_off} $*"; }
fail() { echo "${c_red}error:${c_off} $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || fail "run as root (su -, then run the command again)"
. /etc/os-release
if [ "${ID:-}" != debian ] || [ "${VERSION_ID:-}" != 13 ]; then
  [ "$FORCE" = 1 ] || fail "this installer is made for Debian 13 (found: ${PRETTY_NAME:-unknown}). Use --force to try anyway."
fi

if [ "$IMAGE" = 0 ]; then
  [ -d /run/systemd/system ] || fail "systemd is not running. Docker-style containers are not supported; use a VM, an LXC container or real hardware."
  VIRT="$(systemd-detect-virt -c 2>/dev/null || true)"
  if [ "$VIRT" = lxc ] || [ "$VIRT" = lxc-libvirt ]; then
    say "Running in an LXC container: ISOs are unpacked instead of loop-mounted (needs free disk space equal to the ISO size)"
  fi
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
  xorriso syslinux-common dhcpcd-base wimtools 7zip
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
# Template builds of a pull request install that exact commit (still tracking $CHANNEL for updates).
if [ -n "${PXE_REF:-}" ]; then
  git -C "$DEST" fetch -q origin "$PXE_REF"
  git -C "$DEST" reset -q --hard FETCH_HEAD
fi

say "Configuring the system"
bash "$DEST/deploy/post-update.sh"

if [ "$IMAGE" = 1 ]; then
  # Proxmox LXC template: the admin account, the PXE interface and the passwords are made at the
  # first boot of each container (pxe-boot-server-firstboot.service), not baked into the image.
  install -d -m 755 /etc/pxe-boot-server
  echo "$CHANNEL" > /etc/pxe-boot-server/firstboot
  say "Image prepared ($(cat "$DEST/VERSION"), channel $CHANNEL)"
  return 0
fi

say "Starting"
ARGS=(--channel "$CHANNEL")
[ -n "$IFACE" ] && ARGS+=(--interface "$IFACE")
bash "$DEST/deploy/first-run.sh" "${ARGS[@]}"
}

main "$@"
