#!/usr/bin/env bash
# Finishes an installation: starts the services, creates the administrator account with a
# generated password on a fresh install, and shows where the web interface is.
#
# Run as root by install.sh, and once at first boot of the Proxmox LXC template
# (pxe-boot-server-firstboot.service). All further configuration happens in the web interface:
# the first sign-in opens a short wizard (own password, name, network interface).
#
#   first-run.sh [--channel main|test] [--interface NAME] [--firstboot]
#   first-run.sh --banner-only      rewrite the console banner (used by pxe-helper)
set -euo pipefail
export LC_ALL=C
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
ETC=/etc/pxe-boot-server
CRED="$ETC/initial-admin"
ISSUE=/etc/issue.d/pxe-boot-server.issue
HTTP_PORT=80
APP_USER=pxeboot
CHANNEL=""
IFACE=""
FIRSTBOOT=0
BANNER_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --channel) CHANNEL="${2:?}"; shift 2 ;;
    --interface) IFACE="${2:?}"; shift 2 ;;
    --firstboot) FIRSTBOOT=1; shift ;;
    --banner-only) BANNER_ONLY=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

default_iface() { ip -o route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}'; }
web_ip() {
  local ip
  ip="$(ip -o -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  [ -n "$ip" ] || ip="$(ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)"
  echo "$ip"
}
web_url() { local ip; ip="$(web_ip)"; echo "http://${ip:-<server-ip>}:$HTTP_PORT/"; }

# Console banner (agetty shows /etc/issue.d before the login prompt; \4 is the current IPv4
# address) and a login message, so the details can be found again in the Proxmox console.
write_banner() {
  local user="" pw=""
  if [ -r "$CRED" ]; then
    user="$(sed -n 's/^username=//p' "$CRED")"; pw="$(sed -n 's/^password=//p' "$CRED")"
  fi
  install -d -m 755 /etc/issue.d
  {
    echo "PXE Boot Server  -  http://\\4:$HTTP_PORT/"
    if [ -n "$pw" ]; then
      echo "  Username: $user"
      echo "  Password: $pw"
    fi
    echo
  } > "$ISSUE"
  install -m 755 "$REPO/deploy/motd.sh" /etc/update-motd.d/90-pxe-boot-server
}

if [ "$BANNER_ONLY" = 1 ]; then
  write_banner
  exit 0
fi

# Inside the LXC template the network may still be coming up at first boot.
if [ "$FIRSTBOOT" = 1 ]; then
  for _ in $(seq 1 60); do [ -n "$(default_iface)" ] && break; sleep 1; done
fi
[ -n "$IFACE" ] || IFACE="$(default_iface)"

# Hosts (or containers) without IPv6 cannot listen on [::].
if [ ! -e /proc/net/if_inet6 ] && grep -q 'listen \[::\]' /etc/nginx/sites-available/pxe-boot-server 2>/dev/null; then
  sed -i '/listen \[::\]/d' /etc/nginx/sites-available/pxe-boot-server
fi
systemctl restart nginx

systemctl restart pxe-boot-vlans pxe-boot-server
for _ in $(seq 1 30); do
  curl -fs http://127.0.0.1:8080/healthz >/dev/null 2>&1 && break
  sleep 1
done
curl -fs http://127.0.0.1:8080/healthz >/dev/null || { echo "error: the web app did not start; see: journalctl -u pxe-boot-server" >&2; exit 1; }

SMB_PW=""
if ! pdbedit -L 2>/dev/null | grep -q '^pxe:'; then
  SMB_PW="$(head -c 64 /dev/urandom | base64 -w0 | tr -dc 'A-Za-z0-9' | cut -c1-24)"
  printf '%s\n%s\n' "$SMB_PW" "$SMB_PW" | smbpasswd -s -a pxe >/dev/null
  smbpasswd -e pxe >/dev/null
fi

# Database: create the administrator when there are no accounts yet. Prints
# "NEWADMIN<TAB>username<TAB>password" only when it created one.
NEW="$(cd "$REPO" && sudo -u "$APP_USER" env PYTHONDONTWRITEBYTECODE=1 CHANNEL="$CHANNEL" IFACE="$IFACE" SMB_PW="$SMB_PW" \
  "$REPO/venv/bin/python" - <<'PY'
import os
import secrets
from app import auth, db, dnsmasq
db.migrate()
if os.environ["SMB_PW"]:
    db.set_setting("smb_password", os.environ["SMB_PW"])
if os.environ["CHANNEL"] in ("main", "test") and auth.user_count() == 0:
    db.set_setting("update_channel", os.environ["CHANNEL"])
if os.environ["IFACE"] and not db.get_setting("interfaces"):
    db.set_setting("interfaces", [os.environ["IFACE"]])
if auth.user_count() == 0:
    # Easy to read off a console and type: no 0/O, 1/l/I. 4 groups of 4 = about 90 bits.
    abc = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    pw = "-".join("".join(secrets.choice(abc) for _ in range(4)) for _ in range(4))
    auth.create_user("admin", pw, name="Administrator", role="admin")
    db.set_setting("onboarding", True)
    print(f"NEWADMIN\tadmin\t{pw}")
dnsmasq.apply()
PY
)"
NEW="$(printf '%s\n' "$NEW" | sed -n 's/^NEWADMIN\t//p')"

if [ -n "$NEW" ]; then
  install -d -m 755 "$ETC"
  ( umask 077; printf 'username=%s\npassword=%s\n' "${NEW%%$'\t'*}" "${NEW#*$'\t'}" > "$CRED" )
fi
write_banner
systemctl restart pxe-boot-server
[ "$FIRSTBOOT" = 1 ] && rm -f "$ETC/firstboot"

# The summary: only what is needed to sign in. Everything else is done in the web interface.
URL="$(web_url)"
G=$'\e[32m'; B=$'\e[1m'; O=$'\e[0m'
[ -t 1 ] || { G=""; B=""; O=""; }
echo
echo "${G}${B}PXE Boot Server $(cat "$REPO/VERSION") is ready${O}"
echo
echo "  URL:      ${B}$URL${O}"
if [ -r "$CRED" ]; then
  echo "  Username: ${B}$(sed -n 's/^username=//p' "$CRED")${O}"
  echo "  Password: ${B}$(sed -n 's/^password=//p' "$CRED")${O}"
else
  echo "  Sign in with your existing account (forgotten? run: pxe-reset-admin)"
fi
echo
