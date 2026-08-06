# Config bewerkbaar waar hij hoort, of anders uitleggen waarom niet

Status: plan, 6 augustus 2026. Niet gebouwd. Aanleiding: vink je in de bewerk-wizard een dienst aan die alleen componentconfig heeft, dan gebeurt er niets en legt niets uit waarom.

## Wat een gebruiker meemaakt

Waargenomen op `dimp-r0v` in `modal-edit-services`: sleep-mode en health-check aangevinkt, en alleen de configpagina van sleep-mode verschijnt. Geen foutmelding, geen toelichting, gewoon niets.

Dat is geen bug maar het gevolg van waar een dienst zijn config declareert: `sleep-mode` op `ConfigLayer.PROJECT`, `health-check` op `ConfigLayer.COMPONENT`. De projectbrede stap toont alleen wat op projectniveau te configureren valt. Alleen weet de gebruiker dat niet, en het scherm zegt het niet.

Opnieuw gemeten op 5 augustus, met de registry en niet met bestandsnamen: **zeven diensten dragen componentconfig zonder projectsectie.**

```
publish-on-web, metrics-scraper, health-check, persistent-storage, temp-storage,
user-env-vars, aliases
```

De laatste twee zijn er sinds RC-25 bij gekomen: die zijn systeemdiensten geworden en dragen nu componentconfig. Het probleem groeit dus mee.

## Twee dingen die door elkaar lopen

**Ten eerste ontbreekt de terugkoppeling.** Een aangevinkte dienst die op deze stap niets te bieden heeft, hoort dat te zeggen: "deze dienst stel je per component in", met een verwijzing naar waar dat dan wel kan. Dat is de directe oplossing van de klacht.

**Ten tweede is er een begrippenpaar dat niet klopt.** `ServiceDefinition` heeft een `scope`-veld, en dat lijkt hetzelfde te zeggen als `ConfigLayer`, maar doet dat niet: `keycloak` staat op `scope="component"` terwijl zijn configsectie `ConfigLayer.PROJECT` is. Zolang dat naast elkaar bestaat weet niemand welke van de twee de waarheid is, en een terugkoppeling die op de verkeerde kijkt vertelt de gebruiker iets onjuists.

Zoek dat eerst uit, want het bepaalt hoe punt één gebouwd moet worden. Twee mogelijkheden: het zijn twee verschillende begrippen (dan moeten ze uit elkaar getrokken worden in naam en documentatie), of het is een inconsistentie (dan moet er één weg).

## Voorstel

1. **Uitzoeken wat `scope` en `ConfigLayer` elk betekenen**, vastleggen in `instructions/services.md`, en de uitkomst is een beslissing: samenvoegen of hernoemen. Verifiëren met een test die vastlegt wat de bron van waarheid is voor "waar stel ik deze dienst in".
2. **Per aangevinkte dienst zonder sectie op deze laag een regel tonen**, met waar het wel kan. Afgeleid uit de registry, dus geen dienstnaam in het sjabloon; dat is dezelfde weg als de detailsecties en de acties al gaan.
3. **Nakijken of sommige van die zeven wél een projectsectie horen te hebben.** Niet alles hoort per component: `publish-on-web` en `persistent-storage` hebben mogelijk een zinnige projectbrede instelling. Dat is per dienst een inhoudelijke vraag, geen mechanische.

## Volgorde

1. Eerst de begrippenvraag, want het antwoord bepaalt de rest.
2. Dan de terugkoppeling, met de zeven diensten als testgevallen.
3. Als laatste per dienst beoordelen of er een projectsectie bij hoort. Dat kan ook later; het is de enige stap die inhoudelijke keuzes vraagt.

## Waar op te letten

**Meet via de registry, niet via bestandsnamen.** Twee eerdere inventarisaties van dit soort waren fout omdat ze op de aanwezigheid van een `config_model.py` scanden: `persistent-storage` en `temp-storage` delen er een via `catalog/shared/storage.py` en leken daardoor onvolledig. Het contract zegt dat alleen `__init__.py` verplicht is.

**Systeemdiensten hebben geen vinkje.** `user-env-vars` en `aliases` staan wel in de zeven maar kan een gebruiker niet aanvinken, dus voor hen is de terugkoppeling in de keuzestap niet aan de orde. Ze horen wel bij punt drie: hun componentconfig moet ergens te bewerken zijn.

**Niet elke dienst hoeft een projectsectie.** De oplossing is uitleggen waar iets hoort, niet overal een sectie bijbouwen. Een lege projectsectie is slechter dan een zin die zegt waar je moet zijn.
