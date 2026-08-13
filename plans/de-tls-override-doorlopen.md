# De TLS-override per deployment-component doorlopen

Status: plan, 13 augustus 2026. De generale repetitie van 12 augustus noemde dit expliciet als het enige dat niet getoetst was en dat wel verdient voor de uitrol. De tweede doorloop nam het mee als stap 4b, maar dat was één pad; dit is de doorloop die het vermogen zelf uitput.

## Wat er is

RC-78 gaf de deployment-component-laag zijn eigen editables voor `tls` en `attachment`, gedeclareerd door de dienst zelf (`opi/services/catalog/publish_on_web/`) en verzameld via `config_editables(ConfigLayer.DEPLOYMENT_COMPONENT)`. Ze zijn afgeleid van de bestaande componentvelden met `dataclasses.replace`, dus er is geen tweede kopie.

Wat er per definitie geldt en wat de doorloop moet bevestigen: leeg betekent "volg het component", niet "geen TLS".

## Wat er getoetst moet worden

Dit gaat over een certificaat, dus fouten hier zijn zichtbaar voor de buitenwereld. Loop het hele veld af en niet één gelukkig pad.

1. **Leeg laten verandert niets.** Een deployment zonder override volgt het component, en dat is op het scherm te zien.
2. **Een eigen certificaat op de ene deployment en het platformcertificaat op de andere.** Twee ingressen, met per deployment het juiste certificaat. Meet het certificaat dat er werkelijk uitkomt, niet wat het projectbestand zegt.
3. **`provided` uitzetten met een override.** RC-78 legde met `test_override_can_switch_provided_off` vast dat dit kan; doe het nu op een draaiende deployment en kijk of de ingress meebeweegt.
4. **`provided` zonder attachment.** Het model hoort dat te weigeren; controleer dat de melding zegt wat er ontbreekt en niet dat het projectbestand ongeldig is.
5. **De bijlage is projectbreed.** Twee deployments die naar hetzelfde certificaat wijzen: de verwijdercontrole moet die override meetellen, anders denkt hij dat het certificaat ongebruikt is. Probeer het te verwijderen en kijk wat er gebeurt.
6. **Via de UI en via de API.** De modal is `modal-edit-deployment-<n>`; de API-weg is dezelfde configuratie. Ze horen op hetzelfde uit te komen.
7. **Herverwerken.** Na een override moet een reprocess de ingress opnieuw opleveren met datzelfde certificaat, en niet terugvallen op het component.

## De toets

- elk van de zeven punten hierboven heeft een uitkomst, gemeten en niet afgeleid;
- het certificaat dat een browser krijgt is per deployment het bedoelde, vastgesteld op de verbinding en niet uit het projectbestand;
- een bijlage die door een override gebruikt wordt is niet zomaar te verwijderen;
- het projectbestand valideert na afloop, en er staat nergens een verwijzing naar een bijlage die niet bestaat;
- er is een verslag met per punt wat er gedaan is en wat eruit kwam.

## Waar op te letten

**Meet het certificaat op de verbinding.** `openssl s_client` of gelijkwaardig; wat er in het projectbestand staat is de bedoeling en niet het bewijs. Dat onderscheid is precies waar deze week drie keer iets misging.

**De sandbox draagt een echt certificaat** voor `*.sandbox.rijksapp.dev`. Een eigen certificaat ernaast zetten is dus te onderscheiden van het platformcertificaat, en dat maakt punt 2 meetbaar.

**Repareer niet stilzwijgend.** Vind je iets kapot, leg het vast. Een kleine, evidente fout mag mee; iets dat een besluit vraagt niet.
