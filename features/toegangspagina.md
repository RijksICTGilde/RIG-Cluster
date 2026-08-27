# Toegang: de platformdiensten met hun adres en wachtwoord

Een beheerpagina op `/admin/toegang` die per platformdienst laat zien waar hij staat en hoe je erin komt. Bedoeld voor het moment dat een beheerder ergens in moet en niet wil uitzoeken in welk Secret het wachtwoord staat.

## Wat het is

Drie diensten waar een mens zelf op inlogt: Keycloak, Forgejo en ArgoCD. Per dienst het adres, de gebruikersnaam en het wachtwoord. Adres en gebruikersnaam staan open, het wachtwoord zit achter een onthul-oogje, en alles is met een knop naar het klembord te kopieren. Dezelfde velden als het Keycloak-blok op de projectpagina, zodat het herkenbaar is.

Bij Keycloak komt de OTP-beheerder erbij als die in het geheim staat, met de code van dit moment. Niet de seed: een seed geeft voor altijd codes, de code vergaat binnen dertig seconden.

## Gebruik

Log in als platformbeheerder en kies **Toegang** in het beheerblok van het menu. Het menu-item verschijnt alleen voor een beheerder, maar dat is presentatie: de pagina zelf zit achter `require_platform_admin`, want de URL is de weg naar binnen.

## Hoe het werkt

**Renderen, niet opslaan.** Elke waarde wordt bij het laden van de pagina uit het cluster gelezen. Het alternatief dat op tafel lag was alles een keer in een geaggregeerd geheim zetten, en dat heeft drie nadelen die dit pad niet heeft: er ontstaat een tweede kopie die kan verouderen, er ontstaat een object dat het hele platform waard is om te stelen, en een rotatie moet iemand met de hand doorvoeren. De wachtwoorden staan toch al op het cluster; het waardevolle is de aggregatie, en aggregeren kun je bij het tonen.

**Een cluster zonder een dienst toont geen dode regel.** Ontbreekt het Secret, dan verdwijnt de dienst uit de lijst. Bestaat het Secret wel maar mist het wachtwoordveld, dan blijft de dienst staan met een melding. Dat verschil is precies wat een beheerder moet kunnen zien: "die draait hier niet" is iets anders dan "ik kan er niet in".

**Het adres komt uit de Ingress**, met de `ingress_postfix` uit `cluster_config` als terugval. Zijn de twee het oneens, dan staat er een waarschuwing bij de dienst. Dat is geen schoonheidsfoutje: een dienst die niet onder de postfix van zijn eigen cluster hangt betekent een halve domeinmigratie.

**Geen fragment naast de pagina.** `/admin/diensten` haalt zijn blokken lui op omdat een trage metriekbron de pagina anders ophoudt. Hier zijn het drie `kubectl get secret`-aanroepen die naast elkaar draaien, en een fragment zou een tweede URL zijn waar dezelfde wachtwoorden uitkomen. Een ingang is er een om te bewaken.

**`Cache-Control: no-store`** op het antwoord. Niet `no-cache`: dat laat opslaan nog steeds toe en vraagt alleen om hervalidatie.

**Een auditregel** bij elke keer dat de pagina geopend wordt, met het e-mailadres erbij.

## Waar de waarden vandaan komen

| Dienst | Secret | Gebruiker | Wachtwoord |
|---|---|---|---|
| Keycloak | `keycloak-admin-credentials` | `KEYCLOAK_ADMIN` | `KEYCLOAK_ADMIN_PASSWORD` |
| Forgejo | `forgejo-admin` | `username` | `password` |
| ArgoCD | `argocd-cluster` | vast: `admin` | `admin.password` |

ArgoCD leest bewust **niet** uit onze eigen blauwdruk. `bootstrap/rig-system/kustomize/secrets/templates/argocd-admin-secret.yaml` genereert met `@secret-gen:bcrypt:16`, dus daar staat alleen een hash en daar valt niets uit terug te lezen. De argocd-operator maakt het wachtwoord zelf aan en zet het in `<cr-naam>-cluster` in platte tekst; OPI leest het daar al uit voor zijn eigen aanroepen. Wijst die bron ooit naar `argocd-admin-credentials`, dan toont de pagina een hash alsof het een wachtwoord is, en daar staat een test op.

## Wat er niet in staat

Alles machine-naar-machine: de databaserollen van Forgejo, Keycloak en de mailrelay, het Redis-wachtwoord, het metrics-token, de relay-admin en chisel. Die zijn de meerderheid, en als je ze kwijtraakt genereer je ze opnieuw. Ze hier zetten maakt de lijst lang en daarmee de regels die er wel toe doen onvindbaar. Een test bewaakt dat ze er niet in kruipen.

pgAdmin staat op elk cluster uitgecommentarieerd en staat er daarom niet in.

## Wat deze pagina niet oplost

Hij helpt zolang het cluster leeft. Is het cluster weg, dan is de pagina weg. Voor de geheimen die het cluster overleven (de AGE-sleutel, de versleuteling van de backups) is een eenmalige export bedoeld; die is bewust geparkeerd. Zie `plans/de-installatie-in-drie-fasen-keuzes-geheimen-en-een-overdracht.md`, fase 3a.

## Bestanden

- `opi/services/platform_toegang.py` - de dienstenlijst en het lezen uit het cluster
- `opi/web/router_toegang.py` - de route en de grendel
- `opi/templates_lotc/bg/admin-toegang.html.j2` - de pagina
- `opi/templates_lotc/bg/_toegang-diensten.html.j2` - de diensten, apart zodat er een renderende test op kan staan
- `tests/test_admin_toegang.py`

## Een dienst toevoegen

Zet er een `DienstBron` bij in `DIENSTEN` in `opi/services/platform_toegang.py`. Let op `host_prefix`: die is niet af te leiden uit de naam van de dienst. ArgoCD hangt op dit platform onder `argo` en niet onder `argocd`.
