# Uitgebreide hulp op publish-on-web: het domeinenverhaal

**Status 2026-08-21: optie A gekozen en gebouwd.** `guide_template` op `ServiceDefinition` (generiek, elke service kan volgen), `publish_on_web/guide.md` met de tekst hieronder, `guide`-veld in `GET /api/v2/services/{name}`, portal-render via de bestaande helproute (`/forms/wizard/help/publish_on_web/guide.md`, met link vanuit de popup-help), en de veldbeschrijvingen van base-domain, subdomain, root-component en expose-component-on-bare-domain aangescherpt (config_model + fragmenten geregenereerd, plus de options-source-tekst van base-domain). Zie `features/service-guide.md`. Beslissingen: veld in plaats van subresource (1), guide zonder goedkeuringsbanner (4), links relatief (5), Implementations uitgesteld (6). Nog open: de CLI-kant (2) leeft in de zadctl-codebase.

## Aanleiding

De CLI-help van publish-on-web (help.md plus de veldbeschrijvingen) is niet genoeg om een eigen domein goed in te richten. Concreet gemeten op 2026-08-20: uit de help volgde de route "schrijf je domein in base-domain en zet expose-component-on-bare-domain", terwijl de echte route de combinatie base-domain plus subdomein is, met een subdomein-variant van domain-format, en juist zonder bare-domain. De veldbeschrijving van expose-component-on-bare-domain ("Component served on the bare custom domain") leest als de hoofdroute, maar het is een extra adres erbij.

Het domeinenverhaal (domain-format, subdomein, platformdomeinen versus eigen domein, de aanvraagprocedure, wanneer wel of niet bare-domain) is een ding op zich. Het hoort niet in help.md zelf, want dat is de popup en de korte uitleg in `zadctl service describe`, en je wil de lezer daar niet doodgooien. Het is een "extended help": hoe werkt dit, en hoe doe je bepaalde dingen.

## Wat er nu is

- `services/catalog/publish_on_web/help.md`: korte uitleg, single source voor de portal-popup en het `explanation`-veld van `GET /api/v2/services/{name}` (zie `services/help_text.py`, bewust klein markdown-dialect: titel, secties, alinea's, bullets, bold, links).
- Veldbeschrijvingen in de editables en de `publish-on-web.*.json`-documenten; de CLI toont ze per veld en genereert er `--set`-voorbeelden bij.
- De portalpagina `/eigen-domein` (`templates_lotc/bg/router.html.j2`): het DNS-record, de records op een rij met live IP's, het certificaatverhaal en de internet.nl-punten. Die pagina is host-gebonden en template-gerenderd, dus niet uit de API te lezen.

## Voorstel

Twee delen, samen ingezet:

1. Een tweede markdown per service, bijvoorbeeld `guide.md` naast `help.md`, in hetzelfde kleine dialect. Voor publish-on-web bevat die het domeinenverhaal (voorzet hieronder). Optioneel veld op de servicedefinitie (bijvoorbeeld `guide_template`, naam is een voorstel), dus andere services kunnen volgen maar hoeven niet.
2. Twee veldteksten aanscherpen zodat de grote lijnen ook uit de velden zelf volgen: bij `base-domain` benoemen dat een eigen domein samen met een subdomein-variant en een subdomein het adres vormt, en bij `expose-component-on-bare-domain` benoemen dat dit een extra adres op het kale domein is en niet de route naar een genoemd adres.

## Waar exposen

| Optie | Hoe | Afweging |
|---|---|---|
| A. `guide.md` + API-veld of subresource | `GET /api/v2/services/{name}` krijgt een `guide`-veld, of een subresource `/api/v2/services/{name}/guide`; portal rendert hem via `markdown_to_components` achter een "Meer uitleg"-link in de popup; CLI kan `zadctl service describe <name> --guide` of `zadctl service guide <name>` aanbieden | Single source, drie lezers, popup en describe blijven kort. Klein nieuw mechanisme. Aanbevolen. |
| B. Alles in help.md | Geen nieuw mechanisme | Popup en describe worden lang; precies wat we niet willen. |
| C. Alleen veldteksten | Dichtst bij de velden | Het combinatieverhaal (recept, aanvraag, DNS, certificaat) past niet in een veldbeschrijving. |
| D. Verwijzen naar /eigen-domein | Status quo | Portal-only, niet uit de API te lezen, en die pagina is DNS-gericht, niet configuratie-gericht. |

Aanbeveling: A plus de veldteksten uit deel 2. De guide verwijst voor de DNS-details (records, IP's, internet.nl) naar `/eigen-domein`, zodat de live IP-adressen op precies één plek blijven.

## Open beslissingen

1. Naam en vorm aan de API-kant: een veld `guide` in het bestaande antwoord, of een subresource. Een veld is simpeler; een subresource houdt het detailantwoord klein.
2. CLI-kant: toont `describe` de guide standaard onderaan, achter een vlag, of als apart commando. Voorstel: apart zichtbaar maken (vlag of subcommando) en in de describe-uitvoer één regel die ernaar wijst.
3. Client-neutraal of met zadctl-voorbeelden. Voorstel: neutraal, met veld=waarde-combinaties; de CLI genereert zelf al `--set`-voorbeelden per laag.
4. Of de aanvraagwaarschuwing (approval_specs) ook boven de guide hoort, zoals nu boven de popup-hulp.
5. De link naar /eigen-domein staat relatief in de guide; de portal rendert hem direct, een API-client resolvet hem tegen de portal-basis-URL (bijvoorbeeld https://zad.sandbox.rijksapp.dev/eigen-domein). Als dat te impliciet is kan de API hem absoluut maken bij het serveren.
6. Voor later: een "Implementations"-sectie met codevoorbeelden per taal zou een derde laag zijn (help, guide, implementations). Niet nu bouwen; LLM's genereren zulke voorbeelden inmiddels zelf goed uit de guide plus de velden, dus eerst kijken of er echt vraag naar is.

## Guide-tekst

De tekst is geplaatst als `operations-manager/python/opi/services/catalog/publish_on_web/guide.md`; dat bestand is nu de bron, niet dit plan.
