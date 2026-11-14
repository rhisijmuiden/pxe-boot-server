# De PXE-server over meerdere VLAN's gebruiken met een UniFi UDM-Pro

PXE begint met een broadcast, en die gaat niet van het ene VLAN naar het andere. Er zijn drie manieren om clients in andere netwerken te bereiken. Optie A is het meest compleet: Legacy BIOS en UEFI werken in elk VLAN en je hoeft niets aan de firewall te doen.

## Optie A (aanbevolen): geef de server een poot in elk VLAN

1. Open in UniFi Network de **poortinstellingen** van de switch waar de PXE-server op zit (UniFi Devices > je switch > Port Manager) en laat die poort de gewenste VLAN's als *tagged* netwerken doorgeven, met het hoofdnetwerk van de server als *native*. In recente versies heet dit "Native VLAN / Network" plus "Tagged VLAN Management: Allow All" of een eigen lijst.
2. Gebruik hier in Netwerk & PXE de knop **VLAN-interface toevoegen**: kies de fysieke netwerkkaart, vul het VLAN-ID in en een vrij vast adres in dat VLAN (bijvoorbeeld `10.0.50.5/24`). Reserveer dat adres in UniFi of houd het buiten de DHCP-range.
3. Vink de nieuwe interface aan (bijvoorbeeld `eth0.50`) en klik op **Toepassen**. dnsmasq beantwoordt nu PXE-verzoeken in dat VLAN als proxy DHCP. De UDM-Pro blijft adressen uitdelen.

Clients praten met het adres van de server in hun eigen VLAN. Het verkeer gaat dus niet via de UDM-Pro en je hebt geen firewallregels nodig.

## Optie B: UniFi Network Boot (DHCP-optie 66/67)

1. Ga in UniFi Network naar **Settings > Networks**, open het netwerk van de clients en zet onder **DHCP** de optie **Network Boot** aan (in sommige versies onder "Advanced" of "DHCP Service Management").
2. Server: `<server-ip>`. Bestandsnaam: `snponly.efi` voor UEFI-clients, of `undionly.kpxe` voor Legacy BIOS-clients.
3. Voeg de firewallregels hieronder toe zodat het netwerk deze server kan bereiken.

UniFi geeft elke client in dat netwerk dezelfde bestandsnaam, dus een netwerk gebruikt óf UEFI óf Legacy BIOS. De meeste huidige hardware is UEFI. Onze iPXE-bestanden bevatten een script dat de bootserver uit *next-server* haalt, dus er ontstaat geen lus.

## Optie C: een apart installatie-VLAN met DHCP-relay

1. Maak in UniFi een netwerk zoals "Install" en zet de **DHCP Mode** op **DHCP Relay**, met `<server-ip>` als server.
2. Kies hier **Volledige DHCP-server** en vul een bereik in het installatienetwerk in (bijvoorbeeld `10.0.60.100 - 10.0.60.200`), de UDM-Pro als gateway en je DNS-servers.
3. Voeg de firewallregels hieronder toe. Machines zet je tijdens de installatie in dit VLAN (poortprofiel op de switch).

Legacy BIOS en UEFI werken allebei, omdat deze server per client het juiste bootbestand kiest.

## Firewallregels voor optie B en C

Met de zone-gebaseerde firewall van UniFi Network 9 en nieuwer is verkeer tussen netwerken in de zone *Internal* standaard toegestaan. Heb je netwerken in aparte zones gezet of netwerkisolatie aangezet, maak dan een **Allow**-policy van het clientnetwerk naar deze server (`<server-ip>`), met retourverkeer toegestaan:

|Protocol|Poort|Waarvoor|
|---|---|---|
|UDP|69|TFTP: het iPXE-bootbestand|
|UDP|30000-30100|TFTP-data. dnsmasq gebruikt alleen dit bereik. Sta het toe van deze server naar de clients als retourverkeer niet automatisch wordt herkend.|
|TCP|80|Menu, kernels, installatieprogramma's, antwoordbestanden|
|TCP|445|Windows-installatiebestanden (SMB)|
|UDP|67, 4011|Alleen voor optie C (doorgestuurde DHCP en proxy DHCP)|

*English: [howto-vlan-unifi.md](howto-vlan-unifi.md). Vervang `<server-ip>` door het adres van je PXE-server; in de app staat het al ingevuld.*
