# Een no-mail-beleid op onze DNS-namen, langs dezelfde weg als de CAA-records

De beheerder van het domeinbeleid wijst erop dat `router.rijksapp.nl` niet voldoet aan de verplichte standaarden uit Pas toe of leg uit, en dat dat met losse A/AAAA-records in combinatie met een no-mail-beleid wél haalbaar is. Dat klopt, en het is aan onze kant te repareren met drie ontbrekende records.

## Wat er gemeten is (18 augustus 2026, via dns.google)

```
router.rijksapp.nl   A 147.181.48.71   AAAA 2a04:9a00:1007:4000:0:2:0:8
                     CAA -   MX -   TXT (SPF) -   DMARC -

rijksapp.nl (apex)   CAA  0 issue "letsencrypt.org", 0 issuewild "letsencrypt.org"
                     TXT  "v=spf1 -all"
                     DMARC  "v=DMARC1; p=reject; sp=reject"
                     MX -
```

Het apexdomein heeft het no-mail-beleid dus al. `router.rijksapp.nl` niet, en dat is precies wat de mailtoets op die naam laat vallen.

**SPF erft niet naar subdomeinen.** Een ontvanger die `router.rijksapp.nl` toetst kijkt naar het TXT-record van díe naam, niet naar dat van de apex. En `sp=reject` op de apex doet niets voor een subdomein dat zelf geen DMARC-record heeft: `sp` geldt voor subdomeinen zónder eigen beleid, maar de mailtoets vraagt om een record op de naam zelf. **Verifieer dat laatste voordat je bouwt**, want dit is precies het soort detail waar een aanname duur is: draai de internet.nl-mailtoets op `router.rijksapp.nl` en kijk wat hij precies mist.

## Dat het hier hoort

Er staat sinds deze week een systeem dat precies dit doet voor CAA, en het is algemener gebouwd dan alleen CAA:

| Onderdeel | Bestand |
|---|---|
| welke zones we beheren | `opi/core/dns_config.py` (`MANAGED_DNS_ZONES`) |
| de verzoening bij het opstarten | `opi/core/caa_reconciler.py` |
| lezen en toevoegen bij TransIP | `opi/connectors/transip.py` |
| aanhaken | `opi/core/startup.py:724` |

Drie eigenschappen daarvan wil je hier onveranderd overnemen, want ze zijn er met reden:

1. **Add-only.** Een onverwacht record wordt gelogd en met rust gelaten. DNS is niet de plek om iets weg te gooien waarvan je de herkomst niet kent.
2. **Alleen zones die het account werkelijk houdt.** `list_domains()` wordt vergeleken met wat wij declareren, zodat een typefout nooit iemand anders zijn zone raakt.
3. **Niets doen zonder inloggegevens.** Ontbreken `TRANSIP_ACCOUNT_NAME`/`TRANSIP_PRIVATE_KEY`, dan slaat het over. De sleutel is IP-gebonden en werkt alleen vanaf productie.

Bouw dit dus **niet** als een tweede systeem ernaast. Of de reconciler algemener wordt (records per zone, waarvan CAA er een soort is) of dat er een tweede reconciler bij komt die dezelfde connector en dezelfde poorten gebruikt, is een keuze die je met een reden mag maken; noem hem in de PR.

## Wat er moet komen

Op elke beheerde naam die geen mail verstuurt en niet hoort te ontvangen:

- `TXT  "v=spf1 -all"` op de naam zelf;
- `MX  0 .` op de naam zelf, het null-MX van RFC 7505;
- `TXT  "v=DMARC1; p=reject;"` op `_dmarc.<naam>`.

**Welke namen dat zijn is de eerste ontwerpvraag.** `router.<zone>` is de aanleiding, maar het geldt breder: elke naam die wij in DNS zetten en waarvandaan nooit mail komt. Zoek uit welke namen wij beheren, en declareer ze expliciet in plaats van ze af te leiden. Een lijst die je kunt lezen is hier meer waard dan een regel die slim is.

**Let op de apex.** Die heeft al SPF en DMARC. De reconciler mag daar niets overschrijven; add-only regelt dat, maar toets het.

## Valkuilen

**Null MX naast bestaande MX.** Zet nooit een null-MX op een naam die wél mail ontvangt. Controleer per naam of er al een MX staat en sla die naam dan over, met een logregel.

**TransIP en het recordformaat.** De CAA-code normaliseert zijn inhoud voor de vergelijking (`_normalize_caa`), omdat aanhalingstekens en spaties per API verschillen. Voor TXT en MX geldt hetzelfde: `"v=spf1 -all"` met en zonder quotes is hetzelfde record, en `0 .` en `0 .` met een punt op het eind ook. Zonder normalisatie voegt elke start een dubbel record toe. Dat is de fout die dit onopgemerkt kan laten uitgroeien.

**De naamgeving bij TransIP.** De CAA-code schrijft met `name="@"` voor de apex. Voor `router` en `_dmarc.router` moet daar de relatieve naam staan, niet de volledige. Meet hoe `get_dns_entries` bestaande namen teruggeeft en spiegel dat.

**De reden dat CAA alleen op de apex staat geldt hier niet.** Daar staat in de code: TransIP weigert een record naast een CNAME. `router.<zone>` heeft A/AAAA en geen CNAME, dus een TXT en MX ernaast mag. Bevestig dat op de eerste zone voordat je de rest doet.

## Wat hier NIET bij hoort

- **De CAA-scope.** Dezelfde brief stelt voor om CAA per subdomein te zetten in plaats van op de apex, zodat Let's Encrypt niet op de hele zone mag. Dat is een echte afweging (strakker beveiligen tegen het risico van stil falende vernieuwingen, zie de waarschuwing in `features/caa-records.md`) en verdient een eigen taak. Houd deze taak bij mail.
- Dat `rijksapp.nl` buiten AZ/DPC geregistreerd staat. Organisatorisch, geen techniek.

## Verifieerbaar

- De internet.nl-mailtoets op `router.rijksapp.nl` vóór en ná, met beide uitslagen in de PR.
- Twee keer opstarten voegt niets dubbel toe. Toets dat met een test op de normalisatie, niet alleen door te kijken.
- Een naam met een bestaande MX wordt overgeslagen, met een logregel.
- Zonder TransIP-inloggegevens gebeurt er niets en start OPI gewoon.
- `uv run pytest tests/ -q` groen, plus `ruff check`, `ruff format`, `pyright`.
