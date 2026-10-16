# Enterprise secret- en key management

Status: verkenning, niets gebouwd, niets besloten. Opgeschreven op 16 oktober 2026 naar aanleiding van een gesprek bij een uitvoeringsorganisatie. Dit document legt de vraag en de eerste gedachten vast, zodat wie het later oppakt het opnieuw en grondig kan uitzoeken. Alles hieronder onder "Richting" is een voorstel, geen ontwerp.

## Aanleiding

Die organisatie draait cloudnative workloads op ARO (Azure Red Hat OpenShift) en wil die de komende jaren minder afhankelijk maken van Azure, met daarna een stap naar een soevereine overheidscloud (Fundament). Een concreet voorbeeld is het vervangen van Azure Key Vault door key management binnen ARO waar zij zelf zeggenschap over heeft.

De vraag die daaruit volgt: kan de key management die wij op Fundament bieden ook op zo'n ARO draaien, zodat een organisatie eerst de soevereine subfuncties overzet en de echte verhuizing naar Fundament daarna kleiner en minder riskant wordt? En breder: zoals Haven een standaard is voor het cluster, zou er een standaard (een isolatielaag) moeten zijn voor platformfuncties als key management.

Dit is de eerste casus, niet de enige. Elke organisatie die van een hyperscaler naar een soevereine cloud wil, loopt tegen dezelfde vraag aan, en ZAD zelf heeft hetzelfde probleem in het klein (zie hieronder).

## Wat ZAD vandaag heeft

- Secrets worden met SOPS + AGE versleuteld in git gezet. De AGE-sleutels staan als bestand in `security/` en worden per regelnummer gelezen. `security/key.txt` dekt zowel `local` als `odcn-production`. Zie `plans/sleutels-per-cluster-en-waar-ze-vandaan-komen.md`.
- Onder `infrastructure/bootstrap/infrastructure/vault/` staat een HashiCorp Vault als Flux-`HelmRelease` (chart 0.24.1, standalone, geen HA). OPI gebruikt hem nergens, en de `kustomization.yaml` die hem opneemt verwijst ook naar mappen die niet meer bestaan. Niet in het cluster nagekeken of hij draait.
- Op ODCN is ZAD tenant: etcd-encryptie en een KMS op clusterniveau zijn niet van ons.
- Applicaties krijgen secrets als Kubernetes `Secret`, gegenereerd door OPI. Er is geen rotatie, geen audit trail op het gebruik van een sleutel en geen encrypt/sign-API voor applicaties.

## De gedachten tot nu toe

**De standaard is een interface, geen plugin.** Overdraagbaarheid komt van applicaties die tegen een standaard-API programmeren, niet van een pluginmechanisme. Voor key management bestaan die interfaces al: de OpenBao/Vault-API en de CRD's van External Secrets Operator of de Secrets Store CSI driver voor applicaties, de Kubernetes KMS v2 provider voor etcd, en KMIP en PKCS#11 voor legacy en HSM's. Hetzelfde contract op ARO en op Fundament, met een andere backend, is de isolatielaag waar de vraag naar zoekt.

**Zeggenschap is nog geen soevereiniteit.** OpenBao op ARO geeft eigen beheer, exit-mogelijkheid en geen lock-in, maar de provider beheert nog steeds de hypervisor en het geheugen waar de sleutels in plaintext staan. Technisch los van de provider ben je pas met de root key in een HSM buiten die cloud (hold-your-own-key) en envelope encryption: DEK's lokaal, de KEK extern. Confidential computing verkleint het gat, maar verschuift het vertrouwen naar de chipleverancier en de attestatieketen.

**"Dezelfde dienst" kan twee dingen betekenen.** Dezelfde bouwsteen (software, configuratie, contract) in de eigen tenant van de organisatie is relatief licht. Dezelfde instantie, waarbij workloads op ARO een KMS op Fundament aanroepen, maakt Fundament een kritieke afhankelijkheid: valt de KMS uit, dan ontsleutelt niets meer. Dat vraagt SLA, DR en afspraken over latency en netwerk.

## Richting (voorstel)

1. **OpenBao als bouwsteen**, HA op Raft, met een audit device naar Loki. Niet HashiCorp Vault: dat valt sinds 2023 onder de BSL. OpenBao is de open fork onder de Linux Foundation.
2. **Root of trust in een HSM die de organisatie zelf beheert**, met auto-unseal via PKCS#11 of KMIP. Recovery keys via Shamir verdeeld over meerdere functionarissen, voor scheiding van taken.
3. **Een ZAD-service in de catalogus**, volgens `instructions/services.md`. Een project vraagt key management aan in het projectbestand; OPI richt per project een eigen mount, policy en Kubernetes-auth-rol in die aan de service account van het project hangt. Applicaties gebruiken Transit (encrypt, decrypt, sign zonder dat de sleutel vrijkomt) en krijgen secrets via ESO.
4. **SOPS op OpenBao Transit** (`hc_vault_transit_uri`) in plaats van AGE-bestanden. Dat lost ook het eigen sleutel-in-een-bestand-probleem van ZAD op. Migreren kan geleidelijk omdat SOPS meerdere recipients accepteert.
5. **BIO**: sleutelbeheer valt onder 8.24 (gebruik van cryptografie). Audit trail, rotatie en scheiding van taken maken die control aantoonbaar.

## Open vragen om uit te zoeken

- Wat zit er in Haven+, en werkt iemand anders (Haven-team, Common Ground, de Fundament-cloudstrategie) al aan standaarden voor platformfuncties boven het cluster?
- Welke HSM, wie koopt hem in en beheert hem, en waar staat hij ten opzichte van Fundament en van een ARO-tenant?
- Wat ondersteunt OpenShift voor KMS v2 en etcd-encryptie, en is dat bereikbaar voor een tenant?
- Bouwsteen of instantie: wil zo'n organisatie onze software, of onze dienst? Dat bepaalt de omvang van het werk volledig.
- Welke andere platformfuncties horen in hetzelfde rijtje (identity, certificaten en PKI, logging), en verdient dat één gezamenlijke aanpak?
- Wat betekent dit voor de bestaande secrets van ZAD-projecten: migratiepad, en wie draagt de operationele last van unseal en DR?

## Bronnen

- `plans/sleutels-per-cluster-en-waar-ze-vandaan-komen.md`: hoe de sleutels vandaag per cluster lopen.
- `infrastructure/bootstrap/infrastructure/vault/`: de ongebruikte Vault.
- `instructions/services.md`: hoe een nieuwe service in ZAD inhaakt.
