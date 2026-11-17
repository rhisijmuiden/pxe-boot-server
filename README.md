<p align="center"><img src="branding/logo/rhis-logo.svg" alt="RHIS" height="56"></p>

# PXE Boot Server

*Nederlands: [README.nl.md](README.nl.md)*

A network boot server for Debian 13 that works like Ventoy over the network. Upload ISO files in the web interface, sort them into folders, and boot them on any machine on your network, with **UEFI and Legacy BIOS**.

- **Windows** installers (10, 11, Server) through wimboot, with Setup reading the ISO from a built-in read-only Windows share.
- **Linux**: Ubuntu, Debian (installer and live), Rocky/Alma/RHEL/Fedora, Arch and **Proxmox VE/PBS/PMG**, booted with the right kernel options over HTTP.
- **Any other ISO** (tools, firmware updaters, rescue disks) through iPXE `sanboot`, or `memdisk` for Legacy BIOS.

## Features

| | |
|---|---|
| ISO library | Resumable uploads of large ISOs (chunked, survive a dropped connection), folders become submenus, automatic detection of the boot method. |
| ISO catalog | One-click downloads of Ubuntu, Debian, Rocky, Proxmox VE/PBS, TrueNAS, Clonezilla and Memtest86+, always the newest release, verified against the vendor's checksum. Or download any ISO from a URL. Downloads resume after a restart. |
| Boot menu | Generated from the library. Default entry, timeout, title, and a live preview of the iPXE menu. |
| Live clients | Which client is booting what, firmware type, progress and transfer speed. |
| Statistics | Bandwidth over time, data served per day, running tasks, top ISOs. |
| Answer files | Six-step wizard for unattended installs: Windows `autounattend.xml` (edition, key, local admin, disk, Win11 checks), Ubuntu autoinstall, Debian preseed, Kickstart and Proxmox `answer.toml`. |
| Machines & groups | Link a MAC address or a group of machines to an ISO (and optionally an answer file): next boot only, every boot, or preselected in the menu. Groups can catch new machines by MAC prefix, for example all Proxmox VMs. |
| Network & PXE | Proxy DHCP next to your existing router (default) or a full DHCP server; choose interfaces; add VLAN interfaces. |
| VLAN how-tos | Step-by-step guides for UniFi UDM-Pro, in the app and in [docs/](docs/howto-vlan-unifi.md). |
| Mail server | Your own SMTP server with STARTTLS or SSL/TLS, authentication and a test button; used for password resets and notifications. |
| Info & updates | Version, check for updates and update from the web interface; switch between the `main` (production) and `test` channel. |
| Languages | English and Dutch, switchable per user. Light and dark theme. |

## Install

On a fresh **Debian 13 (trixie)** server or VM, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/pxe-boot-server/main/install.sh | bash
```

Options, passed after `bash -s --`:

```bash
# install the test channel and use eth1 for PXE
curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/pxe-boot-server/main/install.sh | bash -s -- --channel test --interface eth1
```

Then open `http://<server-ip>/` and create the first administrator account.

**Requirements**: Debian 13, amd64, a static IP address (or a DHCP reservation), and enough disk space under `/srv/pxe` for your ISOs. Clients need **Secure Boot switched off** (iPXE and wimboot are not signed by Microsoft).

**Proxmox VE virtual machines**: in our tests UEFI network boot only worked when the VM's EFI disk uses the **2MB OVMF** format. With the 4MB format the VM did not boot from the network. Legacy BIOS (SeaBIOS) VMs work as they are.

### What the installer does

- Installs `nginx`, `dnsmasq`, `samba`, `xorriso`, `syslinux-common`, Python 3 and git.
- Clones this repository to `/opt/pxe-boot-server` and creates a Python virtual environment.
- Creates the users `pxeboot` (runs the web app) and `pxe` (read-only Windows share).
- Copies the iPXE boot files to `/srv/pxe/tftp`, downloads `wimboot`, configures nginx, Samba, dnsmasq (proxy DHCP on the interface with the default route) and systemd.

Running the installer again is safe; it updates the code and the configuration.

## How it works

```
client firmware ──PXE/DHCP──▶ dnsmasq (proxy DHCP) ──TFTP──▶ iPXE (undionly.kpxe / snponly.efi)
iPXE ──HTTP──▶ /ipxe/boot.ipxe ─▶ menu ─▶ chosen ISO
  Windows : wimboot + boot.wim, Setup reads \\server\pxe over SMB
  Linux   : kernel + initrd from the mounted ISO over HTTP
  other   : sanboot (ISO as a virtual CD over HTTP) or memdisk
```

The iPXE files in `ipxe/bin` contain a small embedded script that loads the menu from the server that answered the PXE request (or from `next-server`), so there is no chainload loop and DHCP option 66/67 on a router works as well. Rebuild them with `ipxe/build.sh` or the *Build iPXE loaders* GitHub Action.

| Path | Contents |
|---|---|
| `/opt/pxe-boot-server` | Code (this repository) |
| `/srv/pxe/isos` | Uploaded ISO files and folders |
| `/srv/pxe/mnt/<id>` | Read-only loop mounts of the ISOs |
| `/srv/pxe/tftp` | iPXE boot files |
| `/var/lib/pxe-boot-server` | Database and settings |
| `/var/log/pxe-boot-server` | Update log |

### Ports

| Protocol | Port | Used for |
|---|---|---|
| UDP | 67, 4011 | DHCP / proxy DHCP |
| UDP | 69, 30000-30100 | TFTP |
| TCP | 80 | Web interface, menu, kernels, ISOs |
| TCP | 445 | Windows share for Setup |

## Multiple VLANs

PXE starts with a broadcast that stays inside its VLAN. See **[Using the PXE server across VLANs with a UniFi UDM-Pro](docs/howto-vlan-unifi.md)** for three ways to solve this. The same guide is in the app under *Help & how-tos*.

## Updates and channels

`main` is the production channel and `test` gets changes first. In the web interface, open **Info & updates** in the bottom left, choose the channel, check for updates and click **Update**. The update runs `deploy/post-update.sh` and restarts the app. From the shell:

```bash
sudo /opt/pxe-boot-server/bin/pxe-helper update main   # or: test
tail -f /var/log/pxe-boot-server/update.log
```

## Development

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt pytest httpx
PXE_DEV=1 PXE_DATA_DIR=/tmp/pxedev/data PXE_SRV_DIR=/tmp/pxedev/srv PXE_LOG_DIR=/tmp/pxedev/log \
  uvicorn app.main:app --reload --port 8099
python -m pytest -q tests
```

With `PXE_DEV=1` the root helper is not called and the app serves ISO trees itself. User-facing text lives in `tools/strings.py`; run `python3 tools/strings.py` to regenerate `app/locales/en.json` and `nl.json`.

The house style (colours, fonts, logo) is in [`branding/`](branding/huisstijlgids.md).

## Uninstall

```bash
systemctl disable --now pxe-boot-server pxe-boot-vlans
rm -rf /opt/pxe-boot-server /etc/systemd/system/pxe-boot-*.service /etc/sudoers.d/pxe-boot-server \
       /etc/nginx/sites-enabled/pxe-boot-server /etc/dnsmasq.d/pxe-boot-server.conf
systemctl restart nginx dnsmasq
# ISOs and data stay in /srv/pxe and /var/lib/pxe-boot-server until you delete them.
```
