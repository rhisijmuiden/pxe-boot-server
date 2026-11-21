# PXE Boot Server in a Proxmox LXC container

[Nederlands](proxmox-lxc.nl.md)

The template is a Debian 13 root filesystem with PXE Boot Server installed. Each container
creates its own administrator password, Samba password, SSH host keys and PXE network at first
boot. Requires **Proxmox VE 8.4 or 9.x** (older versions do not know Debian 13).

## 1. Get the template into Proxmox

Datacenter > your node > storage **local** (any storage with content *Container template*) >
**CT Templates** > **Download from URL**:

| Field | Value |
|---|---|
| URL | `https://github.com/rhisijmuiden/pxe-boot-server/releases/latest/download/pxe-boot-server-debian-13-amd64.tar.zst` |
| File name | filled in by **Query URL** |
| Hash algorithm / checksum | optional: SHA-256 from the `.sha256` file on the [release page](https://github.com/rhisijmuiden/pxe-boot-server/releases/latest) |

The address always points to the newest release. Download it again for a newer template; existing
containers update themselves from **Info & updates** in the web interface.

The **Templates** button next to it only lists Proxmox's own appliance catalogue, which third
parties cannot add to, so use **Download from URL**.

From the shell instead:

```bash
wget -O /var/lib/vz/template/cache/pxe-boot-server-debian-13-amd64.tar.zst \
  https://github.com/rhisijmuiden/pxe-boot-server/releases/latest/download/pxe-boot-server-debian-13-amd64.tar.zst
```

## 2. Create the container

**Create CT** with these settings:

| Tab | Setting | Value |
|---|---|---|
| General | Unprivileged container | **on** (recommended) |
| General | Nesting | **on** (needed by systemd in Debian 13) |
| General | Password / SSH key | for `root` on the console and SSH |
| Template | Template | `pxe-boot-server-debian-13-amd64.tar.zst` |
| Disks | Size | **100 GB** or more (see *ISO images* below) |
| CPU / Memory | | 2 cores, 2048 MB, 512 MB swap |
| Network | Bridge | the bridge on the **same network (or VLAN tag) as the clients** that boot |
| Network | IPv4 | static, or DHCP with a reservation in your router |
| Network | Firewall | off, or allow the ports below |

Start the container. Open its **Console**: the login screen shows

```
PXE Boot Server  -  http://192.168.1.20:80/
  Username: admin
  Password: 7kQm-x3Pd-Rt9v-Lw2a
```

Open the address, sign in and finish the short wizard (own password, name, network). After the
wizard the generated password disappears from the console. Lost it? `pct exec <id> -- pxe-reset-admin`.

### All in one command

On the Proxmox host as root:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/pxe-boot-server/main/lxc/proxmox-create-ct.sh)
# options: --ctid 134 --storage local-lvm --disk 150 --bridge vmbr0 --vlan 50 --ip 10.0.50.5/24 --gw 10.0.50.1
```

It downloads the template (checksum checked), creates an unprivileged container with nesting,
starts it and prints the URL, username and password.

## What works differently in a container

| Topic | In an LXC container |
|---|---|
| **ISO images** | Containers cannot loop-mount ISOs (`failed to setup loop device`). The server then unpacks each ISO with 7-Zip to `/srv/pxe/mnt/<id>` automatically. That takes a few minutes and needs **free space equal to the ISO size** next to the ISO itself, so plan roughly twice the size of your ISOs. A separate mount point for `/srv/pxe` (Resources > Add > Mount point) keeps ISOs out of the root disk. |
| **Proxy DHCP and TFTP** | Work in an unprivileged container. The container's network card must be on the same layer-2 network as the clients, because PXE requests are broadcasts that do not cross routers. |
| **Proxmox firewall** | If it is on for the container: allow UDP 67, 69, 4011 and 30000-30100, TCP 80 and 445. |
| **VLANs** | Prefer an extra network card per VLAN in Proxmox (Network > Add, with a VLAN tag) over a VLAN interface inside the container; then tick it under **Network & PXE**. |
| **Windows share (Samba)** | Works unprivileged. |
| **Secure Boot / UEFI / BIOS** | Same as on hardware. |

### Loop mounting anyway (optional, not recommended)

Only a **privileged** container can be given loop devices, which avoids unpacking but weakens the
isolation between container and host. Add to `/etc/pve/lxc/<id>.conf` and restart the container:

```
lxc.cgroup2.devices.allow: b 7:* rwm
lxc.cgroup2.devices.allow: c 10:237 rwm
lxc.mount.entry: /dev/loop-control dev/loop-control none bind,create=file 0 0
lxc.apparmor.profile: unconfined
```

and bind the loop devices (`/dev/loop0` ... `/dev/loop15`) the same way. Unpacking needs none of
this, which is why the template is unprivileged by default.

## Building the template yourself

On Debian 13 as root: `apt install mmdebstrap zstd && bash lxc/build-template.sh --out dist`.
The **LXC template** GitHub workflow does the same on every version change, boots the result once
with systemd-nspawn as a test, and attaches it to the release.
