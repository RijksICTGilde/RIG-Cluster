# Fundament: stand van zaken

Bijgewerkt op 29 augustus 2026. Branch `fundament` op `RijksICTGilde/RIG-Cluster`.

Dit is het overzichtsdocument: waar het staat, wat er af is, en wat er nog moet. De vorige versie was van 20 augustus en beschreef een wereld waarin het cluster niet meer bestond en ArgoCD nooit gesynchroniseerd had. Dat klopt allebei niet meer, en dat is de reden dat het overzicht zoek raakte.

## 1. Waar het om begon

ZAD draait op ODCN. Er kwam een tweede cluster bij op het Fundament-platform en de vraag was wat er nodig is om ZAD daar te laten draaien. Dat bleek uit twee delen te bestaan: aannames in de code die stilzwijgend ODCN veronderstelden, en een cluster dat op een aantal punten anders is.

Sindsdien is er een derde deel bijgekomen, en dat is inmiddels het grootste: de installatie zelf scheiden in configuratie en uitvoering, zodat een volgend cluster geen codewijziging meer kost. Zie `plans/de-installatie-in-drie-fasen-keuzes-geheimen-en-een-overdracht.md`.

## 2. Waar het nu staat

**Het cluster draait en ArgoCD synchroniseert.** Dat was op 20 augustus de belangrijkste openstaande verificatie en die is gehaald. Zeventien Applications staan op Synced en Healthy: vijftien voor het platform, `user-applications`, en één van een echt tenantproject.

Wat er draait: rig-db met zijn databases, Keycloak met SSO-brokering naar de productie-Keycloak en een lokaal `zad-admin`-terugvalaccount, Forgejo als git-achtergrond, MinIO, Redis, Prometheus, cert-manager, CloudNativePG, ingress-nginx, de opslagprovisioner, external-dns tegen TransIP, de mailrelay met een Mailpit-sink, en de operations-manager op `https://zad.fundament-poc.rijksapp.dev`.

Zes certificaten van Let's Encrypt zijn geldig. External-dns beheert de subzone bij TransIP.

**En er staan echte projecten op.** `rig-test-xwf` en `rig-dihf-p3a`. Dat betekent dat de weg van projectbestand tot draaiende pod op dit cluster werkt, en niet alleen de platformlaag.

## 3. Wat er sinds 20 augustus gedaan is

Ruim tweehonderd commits, waarvan dit de brokken zijn die er toe doen.

**De bootstrap kwam op het goede cluster terecht.** Zes taken deden kale `kubectl`-aanroepen en volgden daarmee `current-context`; een bootstrap voor fundament kwam zo op ODCN uit. Ze dragen nu allemaal een expliciete context.

**Sync-waves doen eindelijk iets.** ArgoCD kent geen health check voor een `Application`, dus de root gaf zijn kinderen geen health en elke wave schoof meteen door. Er staat nu een Lua-check in `extraConfig`, en let op dat de application-controller herstart moet worden voordat een wijziging daarin effect heeft.

**De Applications staan niet meer in het `default`-project** maar in `zad-platform`, met een eigen lijst bestemmingen en toegestane clusterresources.

**De mailrelay werkt.** Twee fouten zaten in de weg. De DNS-egressregel stond op poort 53, terwijl CoreDNS op dit Gardener-cluster op 8053 luistert en een NetworkPolicy op de poort van de ONTVANGENDE pod matcht; er kwam dus geen enkele lookup door. En de liveness-probe kilde de pod tijdens een start die zestig seconden duurt, waar nu een startupProbe voor staat.

**De geheimengenerator is een programma geworden.** Hij stond als 250 regels shell in de Taskfile en staat nu in `scripts/generate-secrets.sh`, met zijn invoer uit omgevingsvariabelen. Hij vult ontbrekende velden aan in plaats van een bestaand geheim in zijn geheel over te slaan, en hij kan de geheimen behalve versleuteld naar git ook rechtstreeks op een cluster zetten.

**De clustercatalogus is configuratie geworden.** `CLUSTER_CONFIG` stond als dict van 350 regels in `opi/core/cluster_config.py` en staat nu in `opi/configs/clusters.yaml`, met validatie bij het laden. De omzetting is bewezen door de geladen YAML te vergelijken met de dict zoals die in git stond.

**Er is een beheerpagina Toegang** op `/admin/toegang` met de adressen en wachtwoorden van Keycloak, Forgejo en ArgoCD, live uit het cluster gelezen. Zie `features/toegangspagina.md`.

## 4. Wat er nog moet, op volgorde

1. **De pyzor-fetch uitzetten.** Stalwart probeert bij elke start `public.pyzor.org` te bereiken, het egressbeleid laat dat niet door, en de connect hangt tot de TCP-timeout. Dat kost een minuut per herstart, op fundament gemeten en op ODCN waarschijnlijk hetzelfde (daar niet gemeten, want dat cluster is hier niet bereikbaar). Eén regel in `infrastructure/bootstrap/infrastructure/mail/controller/base/config.toml`, naast de drie fetches die er al uit staan.

2. **De installatiekeuzes vastleggen (fase 1).** De keuzes zitten nu verspreid over `.env-taskfile-{cluster}`, de overlays en `cluster_config`. Er ligt één beslissing onder die alles bepaalt en die nog open staat: wordt `cluster_config` gegenereerd uit de fase-1-configuratie, of ís die configuratie gewoon `cluster_config`.

3. **De generator idempotent maken bij een tweede run per veld.** Deels gedaan (ontbrekende velden worden aangevuld). Wat nog ontbreekt is dat de bestemming "rechtstreeks toepassen" ook in de Taskfile te kiezen is, in plaats van alleen door het script met de hand aan te roepen.

4. **De sandbox migreren** naar de app-of-apps-opzet, en de dan overbodige taken opruimen. Bewust pas hierna, zodat er een werkend voorbeeld staat voordat een dagelijks gebruikt pad verbouwd wordt.

5. **Productie migreren, of bewust niet.** Het runbook staat in `docs/argocd-app-of-apps-migratie.md`, met twee verificatiestappen die een bereikbaar ODCN vragen. De afspraak is voorlopig: productie werkt en blijft zoals het is; fundament en de sandbox zijn de weg vooruit, met de terugval dat productie nog steeds met de huidige opzet bij te werken is.

## 5. Wat er open blijft staan

**Opslag is tijdelijk.** `local-path-provisioner` op de vrije ruimte van de node. Geen snapshots, dus geen PVC-back-ups (database- en bucketback-ups werken wel), geen volume-uitbreiding, en de data staat als mappen op een node. Platformbeheer werkt aan een Rook/Ceph-plugin; die brengt de snapshotclass mee. Tot die er is, is dit een testomgeving en geen plek voor productiedata.

**De invariant tegenover reconciliatie.** Wat je hoe dan ook wilt is dat het wachtwoord in het Secret op het cluster HET echte wachtwoord is. Is git de bron, dan draait een rotatie die je in het cluster doet bij de volgende sync terug; is het cluster de bron, dan brengt niets een verdwenen geheim terug. Bewust nog niet doorgehakt, uitgeschreven in het installatieplan.

**Vier velden ontbreken in `keycloak-admin-secret` op odcn** (`KEYCLOAK_ADMIN_CLIENT_SECRET` en de drie `KEYCLOAK_OTP_ADMIN_*`), die OPI met `optional: true` leest en daar dus stil mist. De generator kan ze aanvullen zonder iets te roteren, maar of die velden daar horen is eerder een keuze dan een reparatie.

**Het plugincontract van fundament heeft twee gaten** die ZAD als eerste raakt. `PluginInstallation.spec.config` is een platte `map[string]string` zonder schema, dus er is geen weg voor geneste installatieconfiguratie en geen formulier ervoor. En er is geen `secretRef`, dus een beheerder kan geen geheim aanleveren.

**`kubectl exec` en `port-forward` werken niet** via `k8s-api.fundament-poc.nl/clusters/<uuid>`; de proxy doet geen protocol-upgrade. OPI raakt dat niet, maar debuggen vanaf een laptop gaat anders, en een meting die exec nodig heeft moet je als Pod met logs uitvoeren.

**De branch is tijdelijk.** De Applications lezen van `fundament`. Gaat het naar main, dan verandert dat op drie plaatsen: de patch onderaan de app-of-apps-kustomization, die in de bootstrap-overlay, en de directe waarde in `argocd-application-infrastructure.yaml`.

## 6. Welk document waarvoor is

Vijf documenten over hetzelfde onderwerp is precies waarom het overzicht zoek raakt. Dit is de verdeling.

| Document | Waarvoor |
|---|---|
| `docs/fundament-stand-van-zaken.md` | dit document: waar het staat en wat er nog moet |
| `docs/cluster-toevoegen.md` | het stappenplan om een cluster toe te voegen |
| `docs/een-nieuw-cluster-installeren.md` | de naslag: welke laag wat bevat, en de vragenlijst voor een nieuw cluster |
| `docs/fundament-cluster-checklist.md` | de metingen aan dít cluster en de gaten die eruit kwamen |
| `plans/de-installatie-in-drie-fasen-keuzes-geheimen-en-een-overdracht.md` | het ontwerp: keuzes, geheimen en de overdracht |
| `docs/argocd-app-of-apps-migratie.md` | het runbook om een draaiend cluster om te zetten |

Openstaand werk dat niet over fundament gaat staat in `TODO.md`, met `TODO_NEXT_RELEASE.md` voor wat op uitrol wacht en `TODO_FUTURE.md` voor wat bewust is uitgesteld.
