# De blast radius van een sleutel

Status: onderzoeksopdracht, 22 september 2026. **Dit is geen bouwtaak.** De uitkomst is een beslisdocument in `features/futures/`, geen code. Wie hieraan werkt en code begint te schrijven, doet het verkeerde.

Aanleiding: de platform-AGE-sleutel is blootgesteld geweest. De rotatie daarvan loopt apart (`de-sops-sleutel-vervangen-met-een-script.md`). Deze opdracht gaat over de vraag die daaronder ligt: waarom opende die ene sleutel alles, en hoe kan dat minder worden zonder het ontwerp van ZAD op te geven.

## Wat er nu staat, gemeten

```
een sleutel  (security/key.txt = k8s secret sops-age-key)
   |
   +-- 19 SOPS-bestanden: argocd-admin, keycloak-admin, postgres, redis, minio,
   |      transip (DNS), vault-init, mail-relay, backup-destination, ...
   |
   +-- config.age-private-key van ELK project
   |      +-- per project: Keycloak-wachtwoorden, api-key, user-env-vars
   |
   +-- repositories[].password van ELK project: de GitHub-PAT, en dat is
          voor elk project DEZELFDE PAT
```

Drie dingen die het onderzoek als uitgangspunt moet nemen, want ze zijn gemeten en niet aangenomen:

1. **OPI heeft clusterbrede rechten op secrets** (`create, get, list, patch, delete` in de ClusterRole). Een oplossing die geheimen per namespace wegzet beschermt dus niets tegen een gecompromitteerde OPI.
2. **Er staat een Vault in de infrastructuur, maar uitgeschakeld.** `infrastructure/bootstrap/infrastructure/vault/` bestaat, en in `clusters/odcn/kustomization.yaml` staan de twee regels uitgecommentarieerd. OPI gebruikt hem nergens; de enige treffer is het woord "vault" in een docstring. Waarom hij uit staat is niet opgeschreven, en dat is de eerste vraag om te beantwoorden.
3. **Alle projecten delen één PAT.** Een lek van één projectbestand geeft toegang tot de repositories van alle projecten.

## De kern: waarom dit moeilijk is

OPI moet elk project kunnen provisioneren. Dat is zijn taak. Dus er moet iets bestaan dat elk projectgeheim kan openen, en dat blijft zo bij elke oplossing. **Compartimentering die dat negeert is schijnveiligheid**, en het onderzoek moet elke voorgestelde richting daarop toetsen.

De winst zit daarom niet in "niemand kan meer alles", maar in de **eigenschappen van die toegang**. Beoordeel elke optie op vier assen, niet alleen op de eerste:

| as | vandaag |
|---|---|
| **omvang** wat opent een lek | alles: infra, elk project, elke PAT |
| **tijd** hoe lang blijft het geldig | eeuwig, ook na rotatie voor oude git-objecten |
| **zichtbaarheid** merk je het | nee, ontsleutelen laat geen spoor na |
| **intrekbaarheid** kun je het stoppen | alleen door alles opnieuw te versleutelen |

Een gestolen age-sleutel scoort op alle vier slecht. Een oplossing die alleen de eerste as verbetert en de andere drie laat staan, is minder waard dan hij lijkt.

## Schijnoplossingen die het onderzoek expliciet moet afwijzen

Deze komen bij dit soort vraagstukken altijd boven en klinken goed. Benoem ze, met de reden:

- **Per-project sleutel afgeleid van een master** (HKDF of vergelijkbaar). Elk project een eigen sleutel, maar wie de master heeft berekent ze allemaal. Geen enkele winst op omvang, wel de illusie ervan.
- **Per-project sleutels die onder één meta-sleutel liggen.** Precies wat we nu hebben, met een extra laag ertussen.
- **Meer recipients toevoegen** zodat meer partijen kunnen ontsleutelen. Dat vergroot de blast radius, het verkleint hem niet.
- **Geheimen per namespace wegzetten** zolang OPI clusterbreed secrets mag lezen. Gemeten: dat mag hij.
- **De sleutel beter verbergen.** Het incident ging niet over een slecht verstopte sleutel maar over een sleutel die in een testbestand belandde. Verbergen schaalt niet tegen menselijke fouten.

## Richtingen om uit te werken

Voor elk: hoe scoort hij op de vier assen, wat kost hij, wat breekt hij, en hoe verhoudt hij zich tot "git is de bron van waarheid".

1. **Domeinen splitsen.** Een sleutel voor de ZAD-infrastructuur, een andere voor de projectsleutels. Goedkoop, geen nieuwe componenten. Verlaagt de omvang van "alles" naar "infra" of "alle projecten", en verandert niets aan tijd, zichtbaarheid of intrekbaarheid. Dit is de ondergrens waar het onderzoek tegenaf moet zetten.
2. **Een broker in plaats van een sleutel.** OPI krijgt niet de sleutel maar het recht om te laten ontsleutelen (Vault transit of vergelijkbaar). De sleutel verlaat de broker nooit. Dat verandert vooral de andere drie assen: elke ontsleuteling is een logregel, het token verloopt, en intrekken is één handeling. Vault staat al in de repo, dus de vraag is niet of het kan maar waarom het uit staat.
3. **Een externe sleutelbeheerder.** SOPS ondersteunt KMS-achtige backends naast age. Zelfde winst als 2, andere afhankelijkheid. Onderzoek wat ODCN hier daadwerkelijk biedt; dat is een feitenvraag en geen ontwerpvraag.
4. **Per-project toegang tot git.** Nu deelt iedereen een PAT. Een GitHub App geeft installation tokens per repository, die kort leven en per project intrekbaar zijn. Dit is een eigen as en kan los van de sleutelvraag opgelost worden.

## Wat "git is de bron van waarheid" hier betekent

Het onderzoek moet die eis scherp krijgen, want hij wordt makkelijk te absoluut gelezen. **De sleutel staat vandaag al niet in git**: hij zit in een Kubernetes-secret en op laptops. De claim "alles is uit git te reconstrueren" is dus nu al onwaar, want zonder die sleutel is de inhoud onbruikbaar.

De echte vraag is niet of er iets buiten git staat, maar **wat er buiten git staat en hoeveel dat is**. Eén sleutel die je moet bewaren is een ander soort afhankelijkheid dan een dienst die moet draaien. Beide zijn een single point of failure, maar met andere faalvormen: een sleutel raak je kwijt of hij lekt, een dienst is onbereikbaar of gecompromitteerd.

Wat het onderzoek moet beantwoorden: welke van die twee faalvormen weegt hier zwaarder, en welke hersteloperatie is acceptabel als het misgaat.

## Hoe anderen dit oplossen

Zoek dit op en vat het samen met een oordeel, niet als opsomming. Minimaal:

- **HashiCorp Vault**: unseal via Shamir, policies per pad, transit-engine zodat de sleutel de kluis nooit verlaat, en een audit log.
- **Sealed Secrets**: één controller-sleutel per cluster. Interessant omdat het precies ons probleem heeft, dus lees hoe zij de blast radius bespreken.
- **External Secrets Operator**: verplaatst het probleem naar een externe store, met per-namespace scoping.
- **SOPS met KMS**: per map of per bestand een andere sleutel, met IAM-policies eromheen.
- **age met meerdere recipients per team**: bedoeld voor mensen, en de vraag is of dat model op automatisering te plakken is.

Let bij elk op: doen zij iets aan de andere drie assen, of alleen aan de omvang?

## Wat deze opdracht oplevert

Een document in `features/futures/`, met:

1. **De vier assen ingevuld** voor de huidige situatie en voor elke richting.
2. **Een aanbeveling met een eerste stap** die op zichzelf waarde heeft, ook als de rest nooit gebeurt. Een voorstel dat pas na zes maanden iets oplevert is hier geen voorstel.
3. **De kosten eerlijk**: wat moet er draaien, wie beheert het, wat gebeurt er als het onbereikbaar is, en hoe ziet herstel na totaal verlies eruit.
4. **Wat er NIET opgelost wordt.** Elke richting laat iets staan; benoem dat, zodat niemand denkt dat het probleem daarmee weg is.
5. **Een open vraag als open vraag.** Waarom staat Vault uit? Als dat niet te achterhalen is uit git of documentatie, schrijf dat op in plaats van te gissen.

## Afbakening

- **Geen code.** Geen scripts, geen manifests, geen proof of concept. Dit is denkwerk dat tot een keuze leidt.
- **De lopende rotatie niet blokkeren.** Die gaat door op de huidige opzet; dit onderzoek gaat over wat daarna anders moet.
- **Niet de git-historie.** Dat oude commits met een oude sleutel te openen blijven is een apart vraagstuk.
