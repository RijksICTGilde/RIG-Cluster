# Upgrade-veiligheidstest: blijven de huidige projectbestanden werken?

Status: plan, 3 augustus 2026. Nog niet gebouwd. Basis: `branches-samenvoegen-naar-main`.

## 1. Aanleiding

De release die nu op `branches-samenvoegen-naar-main` staat verandert veel aan hoe een projectbestand gelezen wordt: elke service heeft een eigen configmodel en schemafragment gekregen, `validate_service_configs` loopt over alle vier de lagen, `project_v2.json` is opgeruimd, en RC-17 voegt een scope-keuze en meerdere schema's toe. Al die wijzigingen zijn per tak groen getest, maar telkens tegen testdata. De vraag die niemand beantwoord heeft is de enige die er voor gebruikers toe doet: gaan de bestaande projectbestanden hier ongeschonden doorheen, of raakt iemand stilletjes iets kwijt.

Dat risico is niet theoretisch. `dp-bn7` viel maandenlang bij elke herverwerking om op een schemagat, zonder dat iemand het zag, en blokkeerde daarmee alle deploys en de auto-tuner van dat project. Die klasse fout is stil: de verwerking faalt, de gebruiker ziet geen foutmelding, en er verandert simpelweg niets meer.

## 2. Wat er al ligt, gemeten op 3 augustus

| Wat | Waar | Wat het doet |
|---|---|---|
| Projectomzetting | `scripts/migrate_project_to_sandbox.py` | Zet `clusters` en per deployment `cluster` en `base-domain` om, herversleutelt `age-private-key` van de productiesleutel naar de sandboxsleutel, laat `config.keycloak` vallen. Neemt meerdere projecten in één aanroep. |
| Projecten aansturen | `scripts/sandbox_project_tool.py` | Haalt een API-sleutel op, verwijdert een project, zet een serviceconfig. Via HTTP met een ondertekend sessiecookie, dus zonder browser. |
| Schemamigratie | `opi/services/schema_migration.py` | `migrate_to_latest()`, `LATEST_SCHEMA_VERSION = 2.6`. |
| Testwerklast | `images/e2e-allservices/` | Statisch Go-binair, bindt elke gekoppelde dienst en meldt het oordeel op `/status`. Sinds 3 augustus publiek op `ghcr.io/minbzk/base-images/e2e-allservices`. |
| Golden-diff patroon | `tests/test_golden_manifests.py` | Byte-vergelijking van gerenderde sjablonen tegen vastgelegde bestanden, connector-vrij en offline. Draait op een verzonnen matrix, niet op echte bestanden. |

Wat er níet is: het omzetscript raakt images en poorten niet aan, en er is geen enkele controle die echte productiebestanden door de nieuwe leesweg haalt.

## 2a. De sleutelgrens, en waarom die het plan stuurt

De server heeft de productiesleutel niet en hoort die ook niet te krijgen. Dat is geen hindernis maar een randvoorwaarde die bepaalt waar elke stap draait.

**De omzetting is een lokale stap.** `migrate_project_to_sandbox.py` ontsleutelt `age-private-key` met de productiesleutel en versleutelt hem opnieuw met de sandboxsleutel. Dat kan alleen op een machine die `security/key.txt` heeft, dus op de werkplek. Wat daarna naar de Forgejo van de server gaat is uitsluitend de omgezette uitvoer: die is versleuteld met de sandboxsleutel en de productiesleutel komt er niet in voor. De server ziet dus nooit productiegeheimen, ook niet versleuteld.

**Laag 1 heeft helemaal geen sleutel nodig.** Migratie en validatie werken op de structuur van het bestand; de AGE-blokken zijn voor dat werk gewoon ondoorzichtige strings die nergens ontsleuteld worden. Die controle kan dus draaien op de productiebestanden zoals ze zijn, zonder enige sleutel, waar dan ook.

**En de bestanden staan voor de server klaar**, in een andere Forgejo-repo dan de sandbox gebruikt: `https://git.claude.robbertuittenbroek.nl/robbert/rig-cluster-projects-github.git`. Op 3 augustus geverifieerd bereikbaar met `git ls-remote`, zonder inloggegevens. Dat is de bron voor laag 1: een ondiepe kloon van die repo, lezen, nooit terugschrijven. Daarmee is laag 1 niet afhankelijk van een lokale checkout en werkt hij overal hetzelfde.

Dat maakt de rolverdeling simpel: omzetten gebeurt lokaal en met de hand, uitvoeren gebeurt op de server met alleen sandboxgeheimen. Het draaiboek moet dat expliciet zeggen, anders zoekt iemand later alsnog naar een manier om die sleutel op de server te krijgen.

## 3. Twee lagen, en de goedkope eerst

De verleiding is om meteen een cluster op te tuigen. Dat is de dure laag en hij dekt maar een steekproef. De goedkope laag dekt alles en draait in seconden, dus die hoort eerst.

### Laag 1: offline replay over alle productiebestanden

Neem alle productie-projectbestanden, haal ze door `migrate_to_latest()` en daarna door de volledige validatieketen van de nieuwe code, en rapporteer per bestand. Geen cluster, geen git, geen SOPS. Dit vangt precies de dp-bn7-klasse: een bestand dat de nieuwe validatie niet haalt en dus bij de eerstvolgende verwerking stil vastloopt.

Belangrijk detail dat eerder is misgegaan: valideer op de gemigreerde data, niet op het rauwe bestand. De verwerking migreert eerst in het geheugen en valideert daarna, dus een controle op het rauwe bestand meet iets anders dan wat productie doet.

Twee vergelijkingen zijn zinvol. De eerste is oud versus nieuw: haalt een bestand het bij de oude code en niet meer bij de nieuwe, dan is dat een regressie die deze release introduceert. De tweede is de migratie-uitkomst zelf: wat verandert `migrate_to_latest()` aan het bestand, en is elke verandering verklaarbaar.

Dit hoort een test te worden die in CI draait, niet een script dat iemand handmatig aanroept, zodat de volgende schemawijziging er automatisch tegenaan loopt.

### Laag 2: echte upgrade op de server-sandbox, met een steekproef

Er is geen ruimte om alle projecten uit te rollen, dus dit is bewust een steekproef. De samenstelling is op 3 augustus bepaald door alle 47 productiebestanden te scannen op diensten, aantal deployments en componenten, en de aanwezigheid van invites.

**De zes projecten van de steekproef:**

| Project | Dep | Cmp | Waarom |
|---|---|---|---|
| `wies` | 18 | 3 | Veruit de meeste deployments; test schaal en de per-deployment paden. |
| `regel-k4c` | 6 | 9 | De meeste componenten, plus `metrics-scraper`. |
| `amt-odc` | 1 | 1 | Zes diensten in een klein project: `minio-storage`, `persistent-storage` en `temp-storage` naast de gebruikelijke. |
| `mozad-dle` | 1 | 1 | Het kale geval: alleen `publish-on-web`. Een project zonder diensten moet net zo goed heel blijven. |
| `openp-4pw` | 1 | 1 | Draagt `invite` én `redis`, de twee diensten die anders helemaal ontbreken, en is met één deployment spotgoedkoop. |
| `dp-bn7` | 1 | 1 | Draagt `invite` en `authorization-wall`, en is het project dat maandenlang stil vastliep op een schemagat. Als er één bestand is dat door deze test moet komen, is het dit. |

Samen dekken die tien van de vijftien diensten: `keycloak`, `postgresql-database`, `publish-on-web`, `persistent-storage`, `temp-storage`, `minio-storage`, `metrics-scraper`, `invite`, `redis` en `authorization-wall`.

**Wat er dan nog niet gedekt is, met de goedkoopste drager erbij**, te overwegen als er ruimte blijkt te zijn: `attachments` (`tva-d62`, 1 deployment, 1 component, maar attachments dragen base64-blokken dus dit is het zwaarste bestand van de twee) en `namespace-postgresql-database` (`algor-odc`, 1 deployment, 3 componenten). Die tweede is inhoudelijk het interessantst, want RC-17 maakt `scope: project` juist gelijkwaardig aan die oude dienst, dus dat is precies het pad dat verandert. Hij kost wel een eigen CNPG-cluster in de sandbox, en dat is waarschijnlijk de reden dat hij er niet bij past.

Niet gedekt en dat kan ook niet: `sleep-mode`, `health-check`, `cross-domain-access` en `resource-tuning` staan in geen enkel productiebestand, want die zijn nieuwer dan de productiedata.

De volgorde is één keer wisselen, niet heen en weer per project:

1. Sandbox op de server, OPI op het image dat productie nu draait (vastgepind, niet `latest`).
2. De vier projecten omzetten en inrichten. Alles laten provisioneren tot alle apps healthy zijn.
3. De gegenereerde staat vastleggen: de zad-deployments-repo op dat moment is het ijkpunt.
4. Het OPI-image wisselen naar de nieuwe build. Eén keer.
5. Elk project heropenen en verversen, zodat alles opnieuw gegenereerd wordt.
6. Vergelijken.

Dat oude OPI een nieuw schema niet kan lezen klopt, maar dat bijt alleen als je teruggaat, en teruggaan hoeft alleen bij afwisselen. Wil je de test herhalen, dan zet je de omgeving schoon opnieuw op; dat is toch wat je wilt voor een eerlijke tweede meting.

## 4. Hoe "klopt het nog" mechanisch wordt

Dit is het scharnierpunt van het hele plan. Zonder dit zit iemand schermen te vergelijken en mist hij precies het stille geval.

De maatstaf is de zad-deployments-repo. Die bevat alles wat OPI voor een project genereert: manifests, secrets, configmaps, RBAC, netwerkbeleid. Leg na stap 3 het commitpunt vast, en doe na stap 5 een `git diff` tegen dat punt. Elke verdwenen omgevingsvariabele, secret-sleutel, ingress, mount of schema verschijnt als een verwijderde regel. Daarmee is "raakt iemand iets kwijt" een leesbare diff.

Wat een diff niet ziet, en waar de live-omgeving juist voor is: databaserechten en `search_path`, of Keycloak-realms, clients en rollen nog kloppen, of buckets en hun beleid er nog zijn, en of ArgoCD alles gesynchroniseerd en healthy krijgt. Daar is de e2e-allservices-probe voor: die bindt elke dienst en meldt per dienst op `/status` of hij hem echt heen en weer krijgt.

Een verschil is niet per se fout. Deze release verandert bewust dingen, bijvoorbeeld de eenmalige migratie naar v2.6. De uitkomst van deze test is dus geen groen vinkje maar een beoordeelde diff: elk verschil is verklaard en gewenst, of het is een bug.

## 5. Wat er gebouwd moet worden

1. **Laag-1-test** die alle productiebestanden migreert en valideert, met een rapport per bestand en een duidelijke uitkomst per klasse fout. Bron is de projecten-repo uit 2a; een ondiepe kloon volstaat, want alleen de huidige inhoud telt.
2. **Uitbreiding van het omzetscript** met twee dingen die het nu niet doet: het image van elk component vervangen door `e2e-allservices`, en de poort meeverhuizen. De probe luistert op 8080 terwijl productiecomponenten eigen poorten declareren, dus zonder die herschrijving falen de gezondheidscontroles. Overweeg dit als aparte vlag zodat het omzetscript ook zonder die vervanging bruikbaar blijft.
3. **Een taakje dat de vergelijking doet**: ijkpunt vastleggen, na de upgrade diffen, en de diff samenvatten naar verdwenen sleutels per project.
4. **Een draaiboek** in `features/` zodat de test herhaalbaar is bij de volgende release, en niet eenmalig werk blijft.

## 6. Open beslissingen

1. ~~Draait laag 1 op de echte productiebestanden of op een kopie?~~ **BESLIST op 3 augustus:** rechtstreeks uit `https://git.claude.robbertuittenbroek.nl/robbert/rig-cluster-projects-github.git`, alleen lezen, nooit terugschrijven. Geen sleutel nodig en geen lokale checkout, dus de test werkt overal hetzelfde en blijft actueel. Wat nog wel te bepalen valt is het gedrag als de repo onbereikbaar is: overslaan met een melding, of falen. Overslaan verbergt een test die stilletjes nooit draait, dus falen heeft de voorkeur tenzij dat een bouwmachine zonder netwerk breekt.
2. **Wat is de uitkomst bij een verschil?** Faalt de test, of produceert hij een rapport dat iemand beoordeelt? Voor laag 1 lijkt falen juist; voor de diff van laag 2 is beoordelen realistischer, want gewenste verschillen bestaan.
3. **Nemen we de projecteigen images mee of vervangen we alles door de probe?** Alles vervangen is sneller en betrouwbaarder, maar test niet of echte werklasten nog starten. Voorstel is vervangen, want het doel is het projectbestand en de dienstverlening, niet de applicaties van gebruikers.
4. **Hoe komt de oude OPI erin?** Een vastgepind image van wat productie draait. Te bepalen of dat de CalVer-tag uit de odcn-overlay is of de commit waar productie nu op staat.
5. **Wat doen we met de vier projecten na afloop?** Laten staan voor een volgende ronde, of opruimen zodat de sandbox leeg blijft. Opruimen via `sandbox_project_tool.py delete` doorloopt de echte teardown en test dus meteen het verwijderpad.
6. ~~Hoort de invite-service erbij?~~ **BESLIST op 3 augustus: ja, en er zijn er twee opgenomen**, `openp-4pw` en `dp-bn7`. Aanleiding is een meting die het risico concreet maakte: `migrate_to_latest()` verplaatst bij vier productieprojecten de invites van top-level `invites:` naar `services/invite/config`, en ruimt bij vier andere projecten stale data op. Dat is een echte herschrijving van bestaande bestanden, dus juist het pad dat bewaakt moet worden. Beide dragers hebben één deployment en één component, dus dit kostte vrijwel geen ruimte.

## 7. Buiten scope

Niet in deze test: de projecteigen applicaties draaiend krijgen, prestatiemetingen, en de productieomzetting zelf. Dit plan levert het bewijs dat de release veilig is voor bestaande bestanden; het uitrollen is een aparte beslissing.
