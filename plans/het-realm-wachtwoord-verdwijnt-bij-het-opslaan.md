# Het realm-wachtwoord verdwijnt bij het opslaan

Status: plan, 13 augustus 2026. **Blokkeert het bewerken via de UI.** Een gewone wijziging op project `tfc-nfv` wordt geweigerd met:

```
configuratie van service 'keycloak' op projectniveau is ongeldig:
1 validation error for KeycloakConfig
realms.0.password  Field required [type=missing,
  input_value={'host': 'https://keycloa..._sandboxed_local_admin'}]
```

Het wachtwoord is dus niet leeg maar **weg**, en het gaat om een waarde die de gebruiker nooit zelf invulde.

## Wat al is ingeperkt

Dit is dezelfde soort fout als de aliaskwestie die RC-88 repareerde: een geheim dat bij het redigeren van de wizardsessie wordt vervangen en daarna niet goed terugkomt. Alleen valt het hier niet terug op een plaatshouder maar verdwijnt het.

De weg loopt via `opi/forms/wizard/secrets.py`:

* `_redact` vervangt elke versleutelde waarde door `REDACTED` voordat de sessie wordt weggeschreven. Het realm-wachtwoord is AGE-versleuteld, dus dat gebeurt hier.
* `restore_redacted_secrets` zet ze bij het opslaan terug uit het opgeslagen project. En daar zit de regel die het verklaart: een plaatshouder **zonder bron laat zijn sleutel vallen** in plaats van hem te schrijven (regel 117 e.v., met de toelichting dat een lijstitem dat het formulier toevoegde geen bron heeft). Dat is bewust, want een plaatshouder mag nooit in het projectbestand belanden.
* Bij een lijst gaat het zoeken van die bron via `_pair_with` (regel 142): eerst op een identificatiesleutel (`name`, `reference`, `realm`, `id`), anders op positie.

`KeycloakRealm` draagt een `realm`-veld, en `realm` staat in die sleutels, dus de paring **zou** moeten werken. Daar houdt mijn zekerheid op.

## Wat er moet gebeuren

**Meet eerst waar de bron wegvalt**, en gok niet. Drie mogelijkheden, en welke het is bepaalt de reparatie:

1. De **paring** faalt: het bewerkte item heeft geen `realm`-sleutel meer (of een andere waarde), zodat `_pair_with` niets vindt en het wachtwoord wordt gedropt.
2. Het **origineel** klopt niet: er wordt teruggezet uit iets dat de realms niet bevat, bijvoorbeeld een projectstructuur van een andere laag.
3. De **redactie** slaat elders toe: het wachtwoord wordt niet als `REDACTED` maar als iets anders bewaard, en valt dan bij het herstellen buiten de regel.

Reproduceer met project `tfc-nfv` op de sandbox: open de bewerkdialoog, wijzig iets dat niets met Keycloak te maken heeft, en sla op. Dat is precies wat de gebruiker deed.

**Het stille deel is het gevaarlijkste.** Deze fout valt nu op omdat de validatie hem tegenhoudt. Was `password` optioneel geweest, dan was het wachtwoord uit het projectbestand verdwenen en had niemand het gemerkt tot de volgende Keycloak-actie. Kijk daarom of er meer velden zijn die dezelfde weg lopen en wél optioneel zijn: `totp_secret` op datzelfde model is er een.

## De toets

- een wijziging via de bewerkdialoog op een project met Keycloak slaat op, zonder dat het realm-wachtwoord verandert of verdwijnt;
- het projectbestand draagt na afloop hetzelfde versleutelde wachtwoord als ervoor, byte voor byte vergeleken;
- hetzelfde voor `totp_secret` en voor elk ander veld dat de redactie raakt;
- er is een test die faalt op de oude code;
- er staat opgeschreven welke van de drie oorzaken het was.

## Waar op te letten

**Herstel de regel niet door plaatshouders toe te laten.** Dat een plaatshouder nooit in het projectbestand mag komen is juist; het probleem is dat de bron niet gevonden wordt.

**Byte-voor-byte vergelijken.** Een AGE-blok dat opnieuw versleuteld wordt ziet er anders uit maar is inhoudelijk gelijk; een wachtwoord dat stilletjes vervangen is ziet er ook anders uit. Vergelijk de ontsleutelde waarde, niet de tekst.

---

## Uitkomst (13 augustus 2026, PR #103)

**Het was oorzaak 1: de paring faalt** — maar niet op de realm, een laag hoger, op de
dienst zelf. `restore_redacted_secrets` loopt beide structuren sleutel voor sleutel
langs, en die spellen een dienst verschillend: de editables geven de oude vorm met de
naam als sleutel terug (`{keycloak: {config: ...}}`), het opgeslagen bestand draagt het
uniforme record (`{name: keycloak, config: ...}`). Bij sleutel `keycloak` staat aan de
bronkant niets, dus alles onder die serviceconfig loopt vanaf daar tegen een lege bron:
`password` en het optionele `totp_secret` vielen allebei weg.

De positieparing kwam hier toevallig goed uit, en het origineel klopte wel — het is
`existing_data`, het project zoals net uit git gelezen.

Reparatie: `apply_modal_edit` normaliseert de ingestuurde data vóór het herstellen, met
dezelfde idempotente `normalize_service_entries` die aan het eind toch al draaide. Plus
een waarschuwingsregel met het pad bij elke plaatshouder die geen bron vindt, want een
terecht laten vallen en een paringsfout zien er in die code hetzelfde uit.

Poorten: `test_a_service_config_edit_keeps_the_secrets_opi_wrote_there` (het bestaande
harnas sloeg de redactiestap over, precies waardoor dit onzichtbaar bleef) en
`tests/e2e/test_realm_secrets_survive_edit.py`, van buitenaf door de echte modal — de
vorm die het misging ontstaat pas in wat de editables teruggeven.
