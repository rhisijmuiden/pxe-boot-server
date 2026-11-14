# Using the PXE server across VLANs with a UniFi UDM-Pro

PXE starts with a broadcast that does not cross VLANs. There are three ways to reach clients on other networks. Option A is the most complete: it supports Legacy BIOS and UEFI on every VLAN and needs no firewall changes.

## Option A (recommended): give the server a leg in every VLAN

1. In UniFi Network, open **Ports** on the switch the PXE server is connected to (UniFi Devices > your switch > Port Manager) and set that port to carry the VLANs you want as *tagged* networks, with the server's main network as *native*. In recent versions this is "Native VLAN / Network" plus "Tagged VLAN Management: Allow All" or a custom list.
2. Here, in Network & PXE, use **Add VLAN interface**: pick the physical interface, enter the VLAN ID and a free static address in that VLAN (for example `10.0.50.5/24`). Reserve that address in UniFi or keep it outside the DHCP range.
3. Tick the new interface (for example `eth0.50`) and click **Apply**. dnsmasq now answers PXE requests in that VLAN as proxy DHCP. The UDM-Pro keeps handing out addresses.

Clients talk to the server's address in their own VLAN, so traffic is not routed through the UDM-Pro and no firewall rules are needed.

## Option B: UniFi Network Boot (DHCP option 66/67)

1. In UniFi Network go to **Settings > Networks**, open the client network, and under **DHCP** enable **Network Boot** (in some versions under "Advanced" or "DHCP Service Management").
2. Server: `<server-ip>`. Filename: `ipxe.efi` for UEFI clients, or `undionly.kpxe` for Legacy BIOS clients.
3. Add the firewall rules below so the network can reach this server.

UniFi sends one file name to every client on that network, so a network uses either UEFI or Legacy BIOS. Most current hardware is UEFI. Our iPXE files include a script that reads the boot server from *next-server*, so there is no chainload loop.

## Option C: a dedicated install VLAN with DHCP relay

1. Create a network such as "Install" in UniFi and set its **DHCP Mode** to **DHCP Relay**, pointing at `<server-ip>`.
2. Here, choose **Full DHCP server** and enter a range in the install network (for example `10.0.60.100 - 10.0.60.200`), the UDM-Pro as gateway and your DNS servers.
3. Add the firewall rules below. Machines are moved to this VLAN (switch port profile) while they are installed.

Both Legacy BIOS and UEFI work, because this server picks the boot file per client.

## Firewall rules for options B and C

With the zone-based firewall of UniFi Network 9 and later, traffic between networks in the *Internal* zone is allowed by default. If you put networks in separate zones or turned on network isolation, add an **Allow** policy from the client network to this server (`<server-ip>`), with return traffic allowed:

|Protocol|Port|Used for|
|---|---|---|
|UDP|69|TFTP: the iPXE boot file|
|UDP|30000-30100|TFTP data. dnsmasq is limited to this range. Allow it from this server to the clients if return traffic is not matched automatically.|
|TCP|80|Menu, kernels, installers, answer files|
|TCP|445|Windows setup files (SMB)|
|UDP|67, 4011|Only for option C (relayed DHCP and proxy DHCP)|

*Nederlands: [howto-vlan-unifi.nl.md](howto-vlan-unifi.nl.md). Replace `<server-ip>` with the address of your PXE server; the in-app version fills it in for you.*
