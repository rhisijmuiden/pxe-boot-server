#!/usr/bin/env bash
# Builds the Proxmox LXC template: a Debian 13 root filesystem with PXE Boot Server installed,
# packed as .tar.zst for Proxmox "CT Templates > Download from URL".
#
#   sudo lxc/build-template.sh [--channel main|test] [--out DIR]
#
# Runs on Debian 13 as root (the GitHub workflow uses a privileged debian:trixie container) and
# needs mmdebstrap and zstd. The admin account, passwords and network are not in the image: each
# container makes its own at first boot (deploy/pxe-boot-server-firstboot.service).
set -euo pipefail
export LC_ALL=C DEBIAN_FRONTEND=noninteractive

HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
CHANNEL=main
OUT="$PWD/dist"
NAME=pxe-boot-server-debian-13-amd64.tar.zst
MIRROR="${MIRROR:-http://deb.debian.org/debian}"
while [ $# -gt 0 ]; do
  case "$1" in
    --channel) CHANNEL="${2:?}"; shift 2 ;;
    --out) OUT="${2:?}"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
command -v mmdebstrap >/dev/null && command -v zstd >/dev/null || { echo "apt install mmdebstrap zstd" >&2; exit 1; }

ROOT="$(mktemp -d /var/tmp/pxe-rootfs.XXXXXX)"
cleanup() {
  for m in dev/pts dev sys proc; do mountpoint -q "$ROOT/$m" && umount -l "$ROOT/$m"; done
  rm -rf "$ROOT"
}
trap cleanup EXIT

# The base system, close to Proxmox's own debian-13-standard template: systemd, ifupdown (Proxmox
# writes /etc/network/interfaces), a DHCP client, SSH and a few tools for the console.
PKGS="systemd,systemd-sysv,dbus,ifupdown,dhcpcd-base,iproute2,iputils-ping,netbase,openssh-server,ca-certificates,curl,wget,less,nano,procps,cron,logrotate,tzdata,locales,apt-utils,bash-completion"
echo "==> Debian 13 root filesystem"
mmdebstrap --mode=root --variant=minbase --include="$PKGS" --components=main \
  --dpkgopt='path-exclude=/usr/share/doc/*' --dpkgopt='path-include=/usr/share/doc/*/copyright' \
  --dpkgopt='path-exclude=/usr/share/man/*' \
  trixie "$ROOT" "$MIRROR"

cat > "$ROOT/etc/apt/sources.list.d/debian.sources" <<'EOF'
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main contrib non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://security.debian.org/debian-security
Suites: trixie-security
Components: main contrib non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
rm -f "$ROOT/etc/apt/sources.list"

mount -t proc proc "$ROOT/proc"
mount --bind /sys "$ROOT/sys"
mount --bind /dev "$ROOT/dev"
mount --bind /dev/pts "$ROOT/dev/pts"
cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"
# No services may start inside the chroot.
printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"; chmod 755 "$ROOT/usr/sbin/policy-rc.d"

echo "==> Updates"
chroot "$ROOT" apt-get -q update
chroot "$ROOT" apt-get -q -y full-upgrade
echo "en_US.UTF-8 UTF-8" > "$ROOT/etc/locale.gen"
chroot "$ROOT" locale-gen >/dev/null

echo "==> PXE Boot Server ($CHANNEL)"
install -m 755 "$HERE/install.sh" "$ROOT/root/install.sh"
chroot "$ROOT" env PXE_REPO="${PXE_REPO:-https://github.com/rhisijmuiden/pxe-boot-server.git}" \
  bash /root/install.sh --channel "$CHANNEL" --image
rm -f "$ROOT/root/install.sh"
VERSION="$(cat "$ROOT/opt/pxe-boot-server/VERSION")"

echo "==> Clean up"
rm -f "$ROOT/usr/sbin/policy-rc.d"
chroot "$ROOT" apt-get -q clean
rm -rf "$ROOT"/var/lib/apt/lists/* "$ROOT"/var/cache/apt/*.bin "$ROOT"/tmp/* "$ROOT"/var/tmp/* "$ROOT"/root/.cache
# Unique per container: Proxmox and systemd create these at first boot.
: > "$ROOT/etc/machine-id"
rm -f "$ROOT/var/lib/dbus/machine-id" "$ROOT"/etc/ssh/ssh_host_*
: > "$ROOT/etc/resolv.conf"
echo pxe-boot-server > "$ROOT/etc/hostname"
find "$ROOT/var/log" -type f -exec truncate -s 0 {} +
for m in dev/pts dev sys proc; do umount -l "$ROOT/$m"; done

echo "==> Pack $NAME ($VERSION)"
mkdir -p "$OUT"
tar --numeric-owner --one-file-system -C "$ROOT" \
  --exclude='./dev/*' --exclude='./proc/*' --exclude='./sys/*' --exclude='./run/*' \
  -cf - . | zstd -q -T0 -19 -o "$OUT/$NAME" -f
( cd "$OUT" && sha256sum "$NAME" > "$NAME.sha256" )
echo "$VERSION" > "$OUT/VERSION"
ls -lh "$OUT"
