#!/usr/bin/env bash
# Brings the system in line with the code in this repository.
# Runs as root from install.sh and after every update from the web interface.
# Safe to run more than once.
set -euo pipefail
export LC_ALL=C
umask 022
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
SRV=/srv/pxe
APP_USER=pxeboot
step() { echo "-- $*"; }

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

step "directories"
install -d -m 755 -o root -g root /etc/pxe-boot-server "$SRV" "$SRV/mnt" "$SRV/tftp"
install -d -m 755 -o "$APP_USER" -g "$APP_USER" "$SRV/isos" "$SRV/boot" "$SRV/cache" "$SRV/derived"
install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$SRV/uploads" /var/lib/pxe-boot-server
install -d -m 755 -o "$APP_USER" -g "$APP_USER" /var/log/pxe-boot-server
chown -R root:root "$REPO"
chmod 755 "$REPO/bin/pxe-helper" "$REPO/bin/pxe-reset-admin" "$REPO/deploy/post-update.sh" "$REPO/deploy/first-run.sh" \
  "$REPO/deploy/motd.sh" "$REPO/install.sh" 2>/dev/null || true
ln -sfn "$REPO/bin/pxe-reset-admin" /usr/local/sbin/pxe-reset-admin
git config --system --get-all safe.directory 2>/dev/null | grep -qx "$REPO" || git config --system --add safe.directory "$REPO"

step "packages"
# wimlib reads the Windows editions from install.wim / install.esd / install.swm
command -v wimlib-imagex >/dev/null || apt-get -o DPkg::Lock::Timeout=600 install -y -q --no-install-recommends wimtools || echo "wimtools could not be installed; Windows editions will not be listed"
# 7-Zip unpacks ISOs where loop devices are unavailable (LXC containers)
command -v 7z >/dev/null || command -v 7zz >/dev/null || apt-get -o DPkg::Lock::Timeout=600 install -y -q --no-install-recommends 7zip || echo "7zip could not be installed; ISOs cannot be unpacked inside a container"

step "python environment"
[ -x "$REPO/venv/bin/python" ] || python3 -m venv "$REPO/venv"
"$REPO/venv/bin/pip" install -q --disable-pip-version-check --upgrade pip
"$REPO/venv/bin/pip" install -q --disable-pip-version-check -r "$REPO/requirements.txt"
"$REPO/venv/bin/python" -m compileall -q "$REPO/app" >/dev/null || true

step "boot loaders (TFTP)"
for f in undionly.kpxe ipxe.efi snponly.efi; do
  install -m 644 "$REPO/ipxe/bin/$f" "$SRV/tftp/$f"
  # dnsmasq adds ".0" to the boot file name for PXE menu entries on some firmware.
  install -m 644 "$REPO/ipxe/bin/$f" "$SRV/tftp/$f.0"
done

step "Secure Boot loaders (signed shim + iPXE)"
# Official signed iPXE builds: Microsoft-signed shim -> iPXE signed by the iPXE project. They cannot
# carry our embedded script, so the same script is served as autoexec.ipxe next to them.
SB="$SRV/tftp/sb"
install -d -m 755 "$SB"
if [ ! -s "$SB/snponly-shim.efi" ] || [ -n "$(find "$SB/snponly-shim.efi" -mtime +30 2>/dev/null)" ] || [ "${PXE_REFRESH_SB:-0}" = 1 ]; then
  TMP="$(mktemp -d)"
  if curl -fsSL --retry 3 -o "$TMP/ipxeboot.tar.gz" https://github.com/ipxe/ipxe/releases/latest/download/ipxeboot.tar.gz \
     && tar -xzf "$TMP/ipxeboot.tar.gz" -C "$TMP" \
     && SBSRC="$(find "$TMP" -type d -name x86_64-sb | head -n1)" && [ -n "$SBSRC" ] && [ -s "$SBSRC/snponly-shim.efi" ]; then
    for f in "$SBSRC"/*.efi; do install -m 644 "$f" "$SB/$(basename "$f")"; done
  else
    echo "warning: could not download the signed iPXE loaders; Secure Boot clients cannot boot until the next update" >&2
  fi
  rm -rf "$TMP"
fi
install -m 644 "$REPO/ipxe/embed.ipxe" "$SB/autoexec.ipxe"

step "wimboot and memdisk"
if [ ! -s "$SRV/boot/wimboot" ] || [ "${PXE_REFRESH_WIMBOOT:-0}" = 1 ]; then
  if curl -fsSL --retry 3 -o "$SRV/boot/wimboot.tmp" https://github.com/ipxe/wimboot/releases/latest/download/wimboot; then
    chmod 644 "$SRV/boot/wimboot.tmp"; mv "$SRV/boot/wimboot.tmp" "$SRV/boot/wimboot"
  else
    rm -f "$SRV/boot/wimboot.tmp"; echo "warning: could not download wimboot; Windows boot will not work until the next update" >&2
  fi
fi
[ -f /usr/lib/syslinux/memdisk ] && install -m 644 /usr/lib/syslinux/memdisk "$SRV/boot/memdisk"
chown -R "$APP_USER:$APP_USER" "$SRV/boot"

step "nginx"
install -m 644 "$REPO/deploy/nginx-pxe-boot-server.conf" /etc/nginx/sites-available/pxe-boot-server
# Servers with IPv6 switched off cannot listen on [::].
[ -e /proc/net/if_inet6 ] || sed -i '/listen \[::\]/d' /etc/nginx/sites-available/pxe-boot-server
ln -sfn /etc/nginx/sites-available/pxe-boot-server /etc/nginx/sites-enabled/pxe-boot-server
rm -f /etc/nginx/sites-enabled/default
nginx -t -q
systemctl enable -q nginx
systemctl reload nginx 2>/dev/null || systemctl restart nginx

step "sudo rule"
visudo -cqf "$REPO/deploy/sudoers"
install -m 440 "$REPO/deploy/sudoers" /etc/sudoers.d/pxe-boot-server

step "samba share"
if [ -f /etc/samba/smb.conf ]; then
  sed -i '/^# >>> pxe-boot-server-global/,/^# <<< pxe-boot-server-global/d' /etc/samba/smb.conf
  sed -i '/^# >>> pxe-boot-server$/,/^# <<< pxe-boot-server$/d' /etc/samba/smb.conf
  # Global options go right after the [global] line, the share at the end of the file.
  sed -i "/^\[global\]/r $REPO/deploy/smb-global.conf" /etc/samba/smb.conf
  cat "$REPO/deploy/smb-share.conf" >> /etc/samba/smb.conf
  testparm -s /etc/samba/smb.conf >/dev/null 2>&1 || { echo "smb.conf check failed" >&2; exit 1; }
  systemctl enable -q smbd
  systemctl reload smbd 2>/dev/null || systemctl restart smbd
fi

step "systemd units"
install -m 644 "$REPO/deploy/pxe-boot-server.service" /etc/systemd/system/pxe-boot-server.service
install -m 644 "$REPO/deploy/pxe-boot-vlans.service" /etc/systemd/system/pxe-boot-vlans.service
install -m 644 "$REPO/deploy/pxe-boot-server-firstboot.service" /etc/systemd/system/pxe-boot-server-firstboot.service
install -d /etc/systemd/system/dnsmasq.service.d
install -m 644 "$REPO/deploy/dnsmasq-override.conf" /etc/systemd/system/dnsmasq.service.d/pxe-boot-server.conf
systemctl daemon-reload
systemctl enable -q pxe-boot-server pxe-boot-vlans dnsmasq
# Only runs when /etc/pxe-boot-server/firstboot exists (the Proxmox LXC template).
systemctl enable -q pxe-boot-server-firstboot

# Existing install: run database migrations and re-apply the PXE configuration when the
# new code renders it differently (for example a changed default boot file).
if [ -f /var/lib/pxe-boot-server/app.db ]; then
  step "PXE configuration"
  (cd "$REPO" && sudo -u "$APP_USER" env PYTHONDONTWRITEBYTECODE=1 "$REPO/venv/bin/python" - <<'PY'
from app import config, db, dnsmasq
db.migrate()
old = config.DNSMASQ_STAGING.read_text() if config.DNSMASQ_STAGING.exists() else ""
if old and dnsmasq.render() != old:
    dnsmasq.apply()
    print("dnsmasq configuration updated")
PY
  ) || echo "warning: could not re-apply the PXE configuration; open Network & PXE and click Apply" >&2
fi

echo "-- post-update done ($(cat "$REPO/VERSION"))"
