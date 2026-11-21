# PXE Boot Server in een Proxmox LXC-container

[English](proxmox-lxc.md)

De template is een Debian 13-rootbestandssysteem met PXE Boot Server erop. Elke container maakt
bij de eerste start zijn eigen beheerderswachtwoord, Samba-wachtwoord, SSH-sleutels en PXE-netwerk
aan. Vereist **Proxmox VE 8.4 of 9.x** (oudere versies kennen Debian 13 niet).

## 1. De template in Proxmox zetten

Datacenter > je node > opslag **local** (of een andere opslag met inhoud *Container template*) >
**CT Templates** > **Download from URL**:

| Veld | Waarde |
|---|---|
| URL | `https://github.com/rhisijmuiden/pxe-boot-server/releases/latest/download/pxe-boot-server-debian-13-amd64.tar.zst` |
| File name | wordt ingevuld door **Query URL** |
| Hash algorithm / checksum | optioneel: SHA-256 uit het `.sha256`-bestand op de [releasepagina](https://github.com/rhisijmuiden/pxe-boot-server/releases/latest) |

Het adres wijst altijd naar de nieuwste release. Download hem opnieuw voor een nieuwere template;
bestaande containers werken zichzelf bij via **Info & updates** in de webinterface.

De knop **Templates** ernaast toont alleen de eigen appliance-catalogus van Proxmox, waar anderen
niets aan kunnen toevoegen. Gebruik dus **Download from URL**.

Of via de shell:

```bash
wget -O /var/lib/vz/template/cache/pxe-boot-server-debian-13-amd64.tar.zst \
  https://github.com/rhisijmuiden/pxe-boot-server/releases/latest/download/pxe-boot-server-debian-13-amd64.tar.zst
```

## 2. De container aanmaken

**Create CT** met deze instellingen:

| Tabblad | Instelling | Waarde |
|---|---|---|
| General | Unprivileged container | **aan** (aanbevolen) |
| General | Nesting | **aan** (nodig voor systemd in Debian 13) |
| General | Password / SSH key | voor `root` op de console en via SSH |
| Template | Template | `pxe-boot-server-debian-13-amd64.tar.zst` |
| Disks | Size | **100 GB** of meer (zie *ISO-bestanden* hieronder) |
| CPU / Memory | | 2 cores, 2048 MB, 512 MB swap |
| Network | Bridge | de bridge op **hetzelfde netwerk (of VLAN-tag) als de clients** die opstarten |
| Network | IPv4 | vast, of DHCP met een reservering in je router |
| Network | Firewall | uit, of sta de poorten hieronder toe |

Start de container en open de **Console**. Het inlogscherm toont:

```
PXE Boot Server  -  http://192.168.1.20:80/
  Username: admin
  Password: 7kQm-x3Pd-Rt9v-Lw2a
```

Open het adres, log in en doorloop de korte wizard (eigen wachtwoord, naam, netwerk). Daarna
verdwijnt het gegenereerde wachtwoord van de console. Kwijt? `pct exec <id> -- pxe-reset-admin`.

### Alles in één commando

Op de Proxmox-host als root:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/rhisijmuiden/pxe-boot-server/main/lxc/proxmox-create-ct.sh)
# opties: --ctid 134 --storage local-lvm --disk 150 --bridge vmbr0 --vlan 50 --ip 10.0.50.5/24 --gw 10.0.50.1
```

Het script downloadt de template (met controle van de checksum), maakt een unprivileged container
met nesting, start hem en toont de URL, gebruikersnaam en het wachtwoord.

## Wat anders werkt in een container

| Onderwerp | In een LXC-container |
|---|---|
| **ISO-bestanden** | Containers kunnen geen ISO's loop-mounten (`failed to setup loop device`). De server pakt elke ISO dan automatisch uit met 7-Zip naar `/srv/pxe/mnt/<id>`. Dat duurt een paar minuten en vraagt **vrije ruimte ter grootte van de ISO** naast de ISO zelf, reken dus op ongeveer twee keer de grootte van je ISO's. Een aparte mount point voor `/srv/pxe` (Resources > Add > Mount point) houdt de ISO's buiten de rootschijf. |
| **Proxy-DHCP en TFTP** | Werken in een unprivileged container. De netwerkkaart van de container moet op hetzelfde layer-2-netwerk zitten als de clients, want PXE-verzoeken zijn broadcasts die niet over routers gaan. |
| **Proxmox-firewall** | Staat die aan voor de container: sta UDP 67, 69, 4011 en 30000-30100 en TCP 80 en 445 toe. |
| **VLAN's** | Liever een extra netwerkkaart per VLAN in Proxmox (Network > Add, met VLAN-tag) dan een VLAN-interface in de container; vink hem daarna aan bij **Netwerk & PXE**. |
| **Windows-share (Samba)** | Werkt unprivileged. |
| **Secure Boot / UEFI / BIOS** | Hetzelfde als op hardware. |

### Toch loop-mounten (optioneel, niet aanbevolen)

Alleen een **privileged** container kan loop devices krijgen. Dan hoeft er niets uitgepakt te
worden, maar de scheiding tussen container en host wordt zwakker. Zet in `/etc/pve/lxc/<id>.conf`
en herstart de container:

```
lxc.cgroup2.devices.allow: b 7:* rwm
lxc.cgroup2.devices.allow: c 10:237 rwm
lxc.mount.entry: /dev/loop-control dev/loop-control none bind,create=file 0 0
lxc.apparmor.profile: unconfined
```

en koppel de loop devices (`/dev/loop0` ... `/dev/loop15`) op dezelfde manier. Uitpakken heeft dit
allemaal niet nodig; daarom is de template standaard unprivileged.

## Zelf de template bouwen

Op Debian 13 als root: `apt install mmdebstrap zstd && bash lxc/build-template.sh --out dist`.
De GitHub-workflow **LXC template** doet hetzelfde bij elke nieuwe versie, start het resultaat één
keer met systemd-nspawn als test en hangt het aan de release.
