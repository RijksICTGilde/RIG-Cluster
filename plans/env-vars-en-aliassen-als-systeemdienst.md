# Env-vars en aliassen als systeemdienst, en config bewerkbaar waar hij hoort

Status: plan, 4 augustus 2026. Niet gebouwd.

## Twee gaten die op hetzelfde neerkomen

Diensten hebben inmiddels een configmodel, editables, een vastgelegd schemafragment en haken voor waar ze zichtbaar zijn. Twee dingen doen precies hetzelfde werk maar staan er buiten, en op twee plekken is die haak niet afgemaakt.

### Gat 1: config die nergens te bewerken is

Gemeten op 4 augustus over alle vijftien diensten: **zeven dragen config op een laag maar hebben nergens een formuliersectie**.

| Dienst | Config op | Sectie op |
|---|---|---|
| `health-check` | component | geen |
| `metrics-scraper` | component | geen |
| `minio-storage` | project, deployment | geen |
| `persistent-storage` | component | geen |
| `publish-on-web` | component | geen |
| `redis` | project | geen |
| `temp-storage` | component | geen |

En twee laagverschillen: `attachments` heeft config op component maar zijn sectie op project, en `cross-domain-access` heeft config op project én deployment maar alleen een projectsectie.

Het gevolg zag je in de bewerk-wizard: vink je een dienst aan die alleen componentconfig heeft, dan gebeurt er niets en legt niets uit waarom. Dat is geen bug in één sectie maar een haak die bij de helft van de diensten niet is ingevuld.

### Gat 2: env-vars en aliassen staan buiten het model

`user-env-vars` staat in het schema op `$defs/component` en `$defs/deployment-component`, als kale eigenschap. Geen configmodel, geen schemafragment, geen editable met validator, geen validatie bij het opslaan. Aliassen idem, op `$defs/component/properties/aliases`.

Terwijl ze qua vorm precies een dienstconfig zijn: ze bestaan op twee lagen met een samenvoeging ertussen (deployment-component wint van component), ze dragen versleutelde waarden, ze horen op bepaalde plekken in de UI zichtbaar te zijn, en ze hebben validatie nodig (twee formaten bij env-vars, minstens één verwijzing bij een alias).

## Waarom systeemdienst en niet gewoon "een dienst"

Een gebruiker moet env-vars niet hoeven aanvinken; elk component heeft ze. Dat was het bezwaar tegen "maak er een dienst van", en dat bezwaar verdwijnt met een begrip dat al bestaat: `ServiceKind.SYSTEM` in `services_enums.py`, met als omschrijving "always runs, never in the list". `Service.is_active` kent het al en `providers.py` filtert er al op.

Een systeemdienst krijgt dus het volle hooksysteem, config model, editables, formuliersecties per laag, een blok op de detailpagina, zonder ooit in de keuzelijst te staan. Dat is precies wat env-vars en aliassen nodig hebben.

De eerste bewoner van dat begrip zou `resource-tuning` worden (`plans/oom-auto-tune-deployment-scoped.md`). Env-vars en aliassen zijn de tweede en derde, en ze bewijzen meteen de kant die daar niet getest wordt: een systeemdienst met een *gebruikersinterface*.

## En aliassen gaan op in env-vars

De richting is dat env-vars de rol van aliassen overnemen. Dat kan sinds 3 augustus: `substitute_known_variables` lost `$VAR` en `${VAR}` op in een user-env-var, tegen alles wat het component ziet. Een alias is daarmee een dunne laag geworden bovenop hetzelfde mechanisme.

Doe dat in deze volgorde, want de laatste stap is onomkeerbaar:

1. Beide als systeemdienst modelleren, met hun eigen configmodel en schemafragment. Ze bestaan dan naast elkaar, gedrag ongewijzigd.
2. De formuliersecties per laag invullen, zodat je ze kunt bewerken waar ze horen: env-vars op component én deployment-component, aliassen op component.
3. Pas daarna beslissen of aliassen verdwijnen. Ze doen één ding dat env-vars niet doen: een alias faalt hard op een onbekende verwijzing (`substitute_variables`), terwijl een env-var mild is omdat een dollar daar vaak gewoon in een wachtwoord staat. Dat verschil is bewust en moet ergens landen voordat je aliassen weghaalt.

## Volgorde en verhouding tot ander werk

Gat 1 is losstaand en kan meteen: zeven formuliersecties invullen plus twee laagverschillen rechtzetten. Dat is repeterend werk waar `keycloak` en `sleep-mode` het voorbeeld voor zijn.

Gat 2 hangt aan `ServiceKind.SYSTEM`, dat nog niet in gebruik is. Wie dit oppakt heeft dus twee keuzes: wachten tot `resource-tuning` dat begrip in gebruik neemt, of het hier in gebruik nemen en dat plan erop laten aansluiten. Het tweede is sneller maar dan moeten beide plannen dezelfde vorm afspreken.

Let op de overlap met `plans/weergaven-naar-de-services.md` (RC-24): dat gaat over de detailpagina, dit over de bewerk-wizard en het model. Zelfde richting, andere haak, dus ze bijten elkaar niet, maar ze raken wel dezelfde diensten.
