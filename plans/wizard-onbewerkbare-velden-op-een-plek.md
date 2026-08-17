# Eén flow voor hoe een wizard met velden omgaat

Status: plan, 5 augustus 2026. Niet gebouwd. Aanleiding: de vraag wat er gebeurt met velden die je niet mag bewerken maar wel terug moeten komen, en de vaststelling dat dat vandaag per geval geregeld wordt in plaats van via één weg.

## De editable is al de bron, maar niet iedereen luistert

Een `Editable` draagt vandaag al alles wat nodig is om drie vragen te beantwoorden:

- **Welk veld toon ik?** `widget`, `label`, `show_when`, `depends_on`
- **Wat is geldig?** `validator`, `required`, `enforcer`
- **Waar staat het in het projectbestand?** `yaml_path`

Dat laatste is het belangrijkste en het minst gebruikte. `yaml_path` zegt letterlijk welk pad deze editable schrijft, bijvoorbeeld `deployments[*]/name` of `components[*]/image`. De verzameling editables van een flow beschrijft daarmee, zonder dat er iets bij hoeft, precies welke paden die flow mag aanraken. Alles daarbuiten is per definitie onbewerkbaar.

Er zijn drie afnemers van die kennis, en ze geven vandaag drie verschillende antwoorden.

| Afnemer | Gebruikt de editables? |
|---|---|
| Tonen (`renderer.py`, secties, flows) | Ja, volledig |
| Valideren via de API (`api/validation.py`) | Half |
| Opslaan (`router_detail_edit.py`) | Nee |

## Waar het uiteenloopt, gemeten

### De API onderhoudt een tweede kopie

`CREATE_PROJECT_DOMAIN_VALIDATORS` doet het goed: dat hergebruikt de gedeelde editables (`DOMAIN_FORMAT_EDITABLE` en de rest). Maar ernaast staan handgeschreven kopieën:

```python
UPSERT_DEPLOYMENT_VALIDATORS: dict[str, Editable] = {
    "deploymentName": Editable(yaml_path="deployments[*]/name", validator=SlugValidator(), required=True),
}
```

Dat pad en die validator bestaan al in de sectie die de wizard toont. Hier staan ze een tweede keer, met de hand. Verandert de regel aan één kant, dan valideert de API iets anders dan het formulier, en niets merkt dat.

### De opslag kijkt helemaal niet naar editables

`router_detail_edit.py` telt **31 vertakkingen op `flow_id`**, met `startswith` en string-vergelijkingen. Die beslissen drie soorten dingen, en alle drie zijn eigenschappen van de flow zelf:

1. **Waar schrijft deze flow naartoe.** `modal-edit-component-3` betekent `components[3]`, en dat wordt uit de tekst teruggerekend met `int(flow_id.removeprefix("modal-edit-component-"))`.
2. **Welke extra context het formulier nodig heeft.** `component_count`, de cross-domain-context.
3. **Of er eerst een plek gemaakt moet worden.** Bij toevoegen wordt een lege deployment aangehecht voordat het formulier bindt.

Het scherpste detail zit in punt 1. In `flows.py` staat:

```python
flow_id=f"modal-edit-component-{component_index}",
```

De index is dus bekend op het moment dat de flow gemaakt wordt. Hij wordt in een tekst gestopt, en daarna in de router weer uit die tekst gepeuterd, op meerdere plekken. Wat een veld op het formulier is (een gestructureerd gegeven) is onderweg een string geworden.

### En daardoor negen stappen voor "de rest"

Omdat de opslag niet weet welke paden bij de flow horen, moet hij alles meenemen en achteraf uitzoeken wat er weg mag. Dat is de keten die er nu staat: vier deep-merge-implementaties (waarvan `deep_merge_into` en `_deep_merge` regel voor regel identiek), `CLEARED_FIELD`-grafstenen omdat `dict.update` geen verwijderde sleutel kan uitdrukken, `_template_only_keys` met een eigen randgeval voor virtuele sleutels, `_apply_list_item_merge`, `_reorder_like` voor de sleutelvolgorde, en sinds 4 augustus `redact_unreachable_secrets` en `restore_redacted_secrets`.

Elk van die stappen staat er omdat er iets stil verdween: een leeggemaakt veld dat terugkwam, elke serviceconfig-wijziging op projectniveau, een tweede serviceconfig-wijziging (`8aace349`, 4 augustus), en versleutelde waarden die op schijf belandden en hun blokopmaak verloren. Vier gaten in dezelfde reis, elk apart gevonden en apart gedicht. Dat patroon voorspelt de volgende.

En de regel die het oplost staat al in `wizard/secrets.py`, alleen toegepast op geheimen:

> Naming the offending fields one by one does not hold. So the rule here is default-deny and derived from the flow itself.

Dat is precies goed, en het geldt net zo goed voor een onversleuteld veld.

## Voorstel: de flow declareert, één weg voert uit

Twee dingen, en het tweede kan pas na het eerste.

**A. Maak van `flow_id` weer een gegeven.** Wat de flow al wist bij het bouwen (welke lijst, welke index, bestaand of nieuw) hoort op `FormFlow` te staan in plaats van in de tekst. `FormFlow` declareert vandaag alleen presentatie: `title`, `mode`, `sections`, `show_review`, `save_per_section`. Daar hoort het doelwit bij, en de contextleveranciers die de flow nodig heeft. Dan verdwijnt het terugrekenen uit de string, en daarmee de meeste van de 31 vertakkingen.

**B. Laat de opslag de editables vragen.** Eén weg voor alle flows: verzamel de `yaml_path` van de actieve editables, dat is de schrijfverzameling, en schrijf alleen die paden weg op het doelwit dat de flow declareert. Wat geen enkele editable noemt wordt niet aangeraakt, en hoeft dus ook niet "behouden" te worden.

Let op wat dit niet is: het projectbestand wordt niet opnieuw opgebouwd en er wordt geen verse git-versie als basis genomen. De samenvoeging blijft op de opgeslagen projectdata; wat verandert is dat de verzameling paden die hij mág aanraken uit de editables komt in plaats van uit negen losse regels achteraf.

**C. De API valideert uit dezelfde verzameling.** Als de schrijfverzameling per flow bestaat, is de API-validatie een afgeleide daarvan in plaats van een tweede lijst. `CREATE_PROJECT_DOMAIN_VALIDATORS` laat al zien hoe dat eruitziet; de handgeschreven kopieën ernaast kunnen dan weg.

## Volgorde

1. **Meet eerst.** Een test die per flow één veld bewerkt, opslaat, en eist dat de rest van het bestand byte-identiek blijft. Dat is het vangnet, en het is de test die alle vier de gaten hierboven had gevonden. Zonder dit verplaats je het risico in plaats van het weg te nemen.
2. **Voeg de identieke merges samen.** `_deep_merge` en `deep_merge_into` zijn hetzelfde; laat er één over. Puur opruimen, geen gedragswijziging.
3. **Doelwit op `FormFlow` (A).** Per flow expliciet maken wat nu uit de string komt, en de router daarop laten lezen. Gedrag ongewijzigd, de 31 vertakkingen slinken.
4. **Schrijfverzameling uit de editables (B).** Haal de bereikbaarheidsberekening uit `secrets.py`, geef hem een eigen plek met `secrets.py` als eerste gebruiker, en laat daarna de opslag hem gebruiken. Hier vervalt `_template_only_keys`.
5. **API-validators afleiden (C).** Als laatste, want dit raakt publiek gedrag en wil je met stap 1 achter je doen.

## Waar op te letten

**`readonly` betekent nu twee dingen.** Op het scherm betekent het "je mag dit niet typen", maar of dat veld de terugreis overleeft is elders geregeld, verspreid over 41 plekken. Na B vallen die twee betekenissen samen. Loop die plekken langs: een veld dat readonly is maar tóch geschreven moet worden, bijvoorbeeld door een hook, is dan een uitzondering die je expliciet moet maken in plaats van iets dat vanzelf goed gaat.

**Paden, geen sleutelnamen.** `password` bestaat op meerdere niveaus. Werkt de schrijfverzameling op naam in plaats van op `yaml_path`, dan is het weer een veldenlijst en zijn we terug bij af.

**Sleutelvolgorde is geen detail.** `_reorder_like` staat er omdat een herordend bestand een enorme diff geeft en het overzicht in de deployments-repo vernielt. Raak je alleen de paden aan die de flow declareert, dan blijft de volgorde vanzelf goed voor de rest, maar test dat expliciet: het is precies de eigenschap die stil kapotgaat.

**Grafstenen blijven nodig.** Een veld dat wél bewerkbaar is mag leeggemaakt worden, en `dict.update` kan dat niet uitdrukken. `CLEARED_FIELD` verdwijnt dus niet; wat verdwijnt is dat hij door de hele keten meereist.
