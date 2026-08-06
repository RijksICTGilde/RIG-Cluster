# Een wizard heeft een basis en mutaties

Status: plan, 6 augustus 2026. Niet gebouwd. Aanleiding: vier bugs op één dag die alle vier dezelfde vraag stelden en er alle vier een eigen antwoord op gaven.

## De vraag die niet één keer beantwoord is

Een wizard heeft twee dingen: een **basis** en een reeks **mutaties**. Bij een nieuw project is de basis leeg en is alles wat je invult een mutatie. Bij bewerken is de basis het bestaande projectbestand en is de mutatie alleen wat je aanraakt. Het eindresultaat is basis plus mutaties.

Dat mechanisme hoort er één keer te zijn. Vandaag wordt het op elke plek opnieuw uitgevonden, en de fouten die dat oplevert zijn niet subtiel: er verdwijnen diensten uit projecten.

## De vier gevallen, gemeten op 6 augustus

**Een vereiste dienst verdween uit het project.** Keycloak vereist publish-on-web, dus die kaart wordt vergrendeld, en vergrendeld betekent `disabled` op de checkbox. Een disabled checkbox verstuurt zijn waarde niet. De POST was dus geen mutatie maar een volledige vervanging, en wat er niet in stond gold als "weggehaald". De dienst viel weg juist omdat hij verplicht is. Gemeten op een echte sessie: de servicesstap had `["keycloak", "invite", "cross-domain-access"]` opgeslagen terwijl het projectbestand publish-on-web wel had.

**De reparatie daarvoor liep in de create-wizard wel en in de bewerk-flow niet.** De server vult ontbrekende afhankelijkheden aan, maar de voorwaarde luidde `section_id == "services"` en de bewerk-stap heet `services-edit` (94478afb).

**Een lezer vond de dienstconfig niet.** De wizard houdt config onder `_services-config`; het opgeslagen project onder `services`. Er is een terugval van het ene naar het andere, maar die werd overgeslagen zodra de genoemde sleutel helemaal ontbrak. Zichtbaar als een lege lijst realm-rollen bij een uitnodiging, terwijl keycloak in dezelfde wizard rollen had (4d4dfedc).

**Een leeg veld schreef een lege lijst.** Het startcommando schreef `command: []` in plaats van niets, en het schema eist minstens één element, dus het project werd afgekeurd. Een reeks volgde `remove_when_none` niet terwijl een los veld dat al wel deed (dd3eb9ed).

Vier keer dezelfde onderliggende vraag: wat is de basis, wat is de mutatie, en hoe zie je het verschil tussen "niet aangeraakt" en "leeggemaakt".

## Wat er nu is, gemeten

```
17 wizard-flows, waarvan 15 modal-edit
39 plekken die create en edit expliciet uit elkaar houden
21 modules die het paar services / _services-config kennen
```

De toestand kent al gereedschap voor dit probleem: `template_data` als momentopname, `step_data` per stap, `get_merged_data()` dat additief samenvoegt, en `CLEARED_FIELD` als grafsteen voor "dit is bewust leeggemaakt". Dat laatste is het bewijs dat het onderscheid nodig is; het is alleen niet consequent doorgevoerd. De grafsteen bestaat voor velden binnen een geïndexeerde lijst, niet voor een dienst die uit een selectie valt.

## Waar het uiteenloopt

**De POST is soms een mutatie en soms een vervanging.** Een formulier stuurt wat het toont. Wat het niet toont (een uitgeklapte sectie, een vergrendeld veld, een stap die je nooit opende) ontbreekt, en ontbreken betekent op de ene plek "ongewijzigd" en op de andere "weg". Dat verschil is nergens vastgelegd.

**Vergrendeld en niet-verstuurd zijn aan elkaar geknoopt.** `disabled` in HTML betekent twee dingen tegelijk: niet aanpasbaar en niet versturen. Wij bedoelen alleen het eerste. Er staat nu een meereizende hidden input om dat te ontkoppelen; dat werkt, maar het is een pleister op een koppeling die er niet had moeten zijn. Het tweede slot (`_locked_services`) zegt in zijn eigen docstring "No longer prevents unchecking" en zet in de code toch `disabled`.

**De basis wordt op twee manieren gelezen.** De wizard leest `_services-config`, het projectbestand `services`. Beide vormen bestaan tegelijk in dezelfde sessie, en elke lezer moet dat zelf weten. 21 modules kennen dat paar.

## Voorstel

1. **Benoem basis en mutatie expliciet in de wizardtoestand.** Nu heet het `template_data` en `step_data`, en of iets een momentopname of een uitgangspunt is, is impliciet. Maak er één begrip van dat beide flows gebruiken: een lege basis voor create, het projectbestand voor edit, en verder identiek.

2. **Eén regel voor "ontbreekt".** Leg vast dat een ontbrekend veld ongewijzigd betekent, en dat weghalen expliciet gebeurt met een grafsteen. Dat mechanisme bestaat al (`CLEARED_FIELD`) maar geldt nu alleen binnen geïndexeerde lijsten. Trek het door naar de selectielijsten, want daar zat het duurste gat.

3. **Ontkoppel vergrendeld van niet-verstuurd.** Een vergrendeld veld hoort een gewone waarde te versturen; het slot is een UI-eigenschap. Daarmee vervalt de meereizende hidden input. De server blijft de bewaker: hij vult vereiste diensten aan en weigert een selectie die niet klopt, want dat is waar die regel thuishoort.

4. **Eén weg naar de dienstconfig.** Een lezer hoort niet te weten of hij in een wizard of in een projectbestand kijkt. Dat is precies wat `smart_get_value` wil zijn; maak dat de enige weg en haal de 21 plekken die het paar zelf kennen daar naartoe.

5. **Beide flows door dezelfde opslagstap.** Create en edit lopen nu uiteen, tot in het opruimen van de sessie toe (de create-flow gooit hem weg vóór het werk gedaan is, zie `plans/de-wizard-levert-geen-ongeldig-projectbestand-op.md`). Eén pad, met het verschil alleen in de basis.

## Volgorde

1. De inventarisatie van de 39 plekken die create en edit uit elkaar houden: welke gaan echt over de basis, en welke zijn toeval. Die uitkomst bepaalt of dit een middelgrote of een grote klus is, en hoort in dit plan te landen voordat er iets verandert.
2. Basis en mutatie als begrip, zonder gedragsverandering. Verifiëren: de suite blijft groen en `get_merged_data` levert hetzelfde op.
3. De regel voor "ontbreekt", met de selectielijst als eerste bewoner. Verifiëren: een vergrendelde dienst overleeft een opslag ook zonder hidden input.
4. Het slot ontkoppelen, en de hidden input weghalen.
5. De lezers naar één weg, in groepen, met de suite groen na elke groep.

## Waar op te letten

**Dit is een verbouwing van de kern, geen opruiming.** Elke wizard hangt eraan, en een fout hier is niet zichtbaar als een foutmelding maar als een dienst die stilletjes uit een projectbestand verdwijnt. Doe het in groepen en houd de suite groen na elke groep.

**Een browsertest vangt hier wat een unittest niet ziet.** Drie van de vier bugs waren onzichtbaar in een render-test: ze ontstonden pas bij het versturen, of alleen in de bewerk-flow, of alleen nadat de gebruiker iets aanvinkte. Wat hier gebouwd wordt hoort e2e gedekt te zijn.

**Verifieer op een echte sessie.** Twee van de vier bugs zijn gevonden door het sessiebestand van de pod te lezen (`/data/wizard-sessions/*.json`), niet door redeneren. Een gereconstrueerde toestand toonde het probleem niet.

**Uniformiteit is geen doel op zich.** Sommige verschillen tussen create en edit zijn echt: bij create bestaat het project nog niet, dus een naam mag nog veranderen en een component moet er zijn. Het doel is dat een verschil bedoeld en zichtbaar is, niet dat alles gelijk wordt.
