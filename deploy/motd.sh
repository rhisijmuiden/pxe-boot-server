#!/bin/sh
# PXE Boot Server login message (installed to /etc/update-motd.d/90-pxe-boot-server).
ip="$(ip -o -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
echo
echo "PXE Boot Server $(cat /opt/pxe-boot-server/VERSION 2>/dev/null)"
echo "  URL:      http://${ip:-<server-ip>}:80/"
if [ -r /etc/pxe-boot-server/initial-admin ]; then
  echo "  Username: $(sed -n 's/^username=//p' /etc/pxe-boot-server/initial-admin)"
  echo "  Password: $(sed -n 's/^password=//p' /etc/pxe-boot-server/initial-admin)"
fi
echo
