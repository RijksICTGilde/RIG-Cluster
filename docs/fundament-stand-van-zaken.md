# Fundament: stand van zaken

Afgerond op 20 augustus 2026. Branch `fundament` op `RijksICTGilde/RIG-Cluster`.

Dit document is bedoeld voor wie dit oppakt: wat er gedaan is, waar het staat, en wat er nog moet. De onderliggende documenten zijn `docs/cluster-toevoegen.md` (het stappenplan), `docs/fundament-cluster-checklist.md` (de metingen aan dit cluster) en `plans/operators-via-argo-in-plaats-van-taken.md` (de ontwerpkeuze).

## 1. Waar het om begon

ZAD draait op ODCN. Er kwam een tweede cluster bij op het Fundament-platform, en de vraag was wat er nodig is om ZAD daar te laten draaien. Het antwoord bleek uit twee delen te bestaan: een reeks aannames in de code die stilzwijgend ODCN veronderstelden, en een cluster dat op een aantal punten anders is.

## 2. Wat er gedaan is

**Het cluster is gemeten in plaats van aangenomen.** Twaalf vragen uit de vragenlijst van `een-nieuw-cluster-installeren.md` zijn beantwoord met een commando in plaats van een mail. Het is vanilla Kubernetes 1.35.6 (Gardener op metal-stack), geen OpenShift, geen policy-engine, geen Pod Security Admission, geen Capsule, en wij hebben cluster-admin. Egress staat open, dus er is geen registry-mirror nodig. VPA draait en levert echte aanbevelingen, wat beter is dan de sandbox.

**Vier aannames in de code zijn eruit gehaald.** De AGE-sleutel viel op zes plaatsen terug op de ODCN-productiesleutel, zonder waarschuwing. De UID-keuze hing aan een lijstje clusternamen in drie templates, waardoor elk nieuw cluster in de OpenShift-tak viel en pods zonder UID of `fsGroup` kreeg. De snapshotclass had een ODCN-naam als vangnet, waardoor een cluster zonder snapshots stil op een niet-bestaande klasse viel. En de tenant-namespaces kregen onvoorwaardelijk een Calico-egressannotatie mee die alleen op ODCN betekenis heeft.

**De infrastructuur is omgezet naar app-of-apps.** Dertien Applications met sync-waves in plaats van een platte lijst, zodat volgorde uitgedrukt kan worden. Dat was nodig om de operators (cert-manager, CloudNativePG, ingress-nginx en de opslagprovisioner) uit de Taskfile naar GitOps te halen: een CRD moet geregistreerd zijn voordat een custom resource hem gebruikt.

**De bootstrap is teruggebracht tot zijn ondergrens.** `task cluster:bootstrap` doet de AGE-sleutel, de secrets, de git-credentials, de namespace, het `sops-age-key` secret, de ArgoCD-operator en de bootstrap zelf. Meer niet; alles daarboven komt uit git. De operations-manager zat eerst in de bootstrap en is nu een Application op wave 4, omdat hij afhangt van dingen die pas via GitOps komen.

**En er is opgeschreven wat we onderweg leerden**, in `docs/cluster-toevoegen.md`.

## 3. Waar het nu staat

**Het cluster bestaat niet meer.** `functl cluster list` geeft "No clusters found" en de API antwoordt `Forbidden`. De organisatie `zad` staat er nog. Waarschijnlijk is de PoC opgeruimd; opslag was daar nog work in progress.

Dat is minder erg dan het klinkt, want al het werk is code en configuratie, en `cluster:bootstrap` is er juist op gebouwd om tegen een vers cluster te draaien. Maar het betekent wel iets voor wat er wel en niet bewezen is.

**Wat bewezen is, tegen het echte cluster:** de platformlaag installeert en werkt (ingress bereikbaar van buiten met HTTP 200 door een echte Ingress, een PVC die bindt en beschrijfbaar is door een non-root pod met de securityContext die onze template rendert, cert-manager en CNPG draaiend). Verder dat metal-ccm automatisch een publiek IP toewijst, dat `kubectl exec` en `port-forward` niet werken via de API-proxy, en dat de meegeleverde `csi-lvm` niets kan provisionen omdat zijn device-patroon de systeemschijf meepakt.

**Wat niet bewezen is:** ArgoCD heeft nooit gesynchroniseerd. De dertien Applications, de waves, de bootstrap-reeks en de app-of-apps zijn geverifieerd met `kustomize build` en met de tests, maar nooit door een draaiende ArgoCD heen gehaald. Dat is de belangrijkste openstaande verificatie.

## 4. Wat er nog moet, op volgorde

1. **Een cluster.** Zonder cluster kan stap 2 en verder niet.
2. **Een GitHub-token.** `cluster:bootstrap` vraagt er vanzelf om. Neem een eigen token voor dit cluster.
3. **Een wildcard-DNS-record** `*.fundament-poc.rijksapp.dev` naar het adres van de ingresscontroller. Daarmee resolvet alles eronder en werkt Let's Encrypt via HTTP-01. Let op dat het toegewezen IP ephemeer is.
4. **De eerste sync draaien en volgen.** Dit is de echte test. Let vooral op of de repo-server de remote kustomize-URL's van de operators kan ophalen; dat is nergens eerder gedaan en het alternatief is de manifests in de repo zetten.
5. **De sandbox migreren** naar dezelfde opzet, en de dan overbodige taken opruimen. Bewust pas hierna, zodat er een werkend voorbeeld staat voordat een dagelijks gebruikt pad verbouwd wordt.

## 5. Wat er open blijft staan

**Opslag is tijdelijk.** `local-path-provisioner` op de vrije ruimte van de node. Geen snapshots, dus geen PVC-back-ups (database- en bucketback-ups werken wel), geen volume-uitbreiding, en de data staat als mappen op een node. Platformbeheer werkt aan een Rook/Ceph-plugin; dat is de vervanging en die brengt de snapshotclass mee. Tot die er is, is dit een testomgeving en geen plek voor productiedata.

**external-dns staat klaar maar uit.** Hij heeft het `transip-credentials` secret nodig, en dat zit alleen in de odcn-overlay versleuteld met de ODCN-sleutel. Met een wildcard-record is hij voorlopig ook niet nodig. Zet je hem aan, dan is een eigen `--txt-owner-id` niet optioneel: zonder dat verwijderen twee external-dns-instanties elkaars records in dezelfde zone, en dat is de DNS van `rijksapp.dev` in productie.

**De branch is tijdelijk.** De Applications lezen van `fundament` zolang dit werk daar leeft. Gaat het naar main, dan verandert dat op twee plaatsen: de patch in de app-of-apps en die in de bootstrap-overlay. De datarepo's blijven hoe dan ook op main.

**`kubectl exec` en `port-forward` werken niet** via `k8s-api.fundament-poc.nl/clusters/<uuid>`; de proxy doet geen protocol-upgrade. OPI raakt dat niet (die gebruikt geen van beide), maar debuggen vanaf een laptop gaat anders, en de Forgejo-init van de sandbox zou hier niet werken.
