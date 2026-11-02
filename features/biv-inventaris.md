# BIV-inventaris

De BIV-classificatie van de ZAD-componenten staat op één machine-leesbare plek:
`architecture/biv-inventaris.yaml`. Daaruit volgen een mermaid-componentdiagram en een
BIV-plus-mitigatie-tabel in `architecture/biv-overzicht.md`, en een check die een gat laat
opvallen. Diagram, tabel en toets komen zo uit dezelfde bron en kunnen niet uit elkaar
lopen.

BIV staat voor beschikbaarheid, integriteit en vertrouwelijkheid (CIA in het Engels). De
score per as bepaalt het BBN (Basisbeveiligingsniveau), en dat BBN bepaalt welke
BIO2-maatregelen van toepassing zijn en hoe streng. De classificatie is daarmee geen los
onderdeel naast de baseline, maar wat de baseline stuurt.

## Gebruik

```bash
task biv-overzicht   # genereer architecture/biv-overzicht.md opnieuw
task biv-check       # toets de inventaris op gaten en het overzicht op drift
```

Bewerk `architecture/biv-overzicht.md` nooit met de hand: een wijziging hoort in de
inventaris. `tests/test_biv_inventaris.py` meldt het verschil, en de CI-job
`biv-inventaris` in `.github/workflows/ci.yml` doet hetzelfde.

## De velden van een component

```yaml
componenten:
  - id: gedeelde-postgresql          # kleine letters, cijfers en streepjes
    naam: Gedeelde PostgreSQL
    gegevens: De databases van alle projecten die geen eigen clusterdatabase kozen.
    trust-boundary: gedeelde diensten # wordt een subgraph in het diagram
    aanvalsproef: true                # doel van de aanvalsproef
    beschikbaarheid: H                # L, M of H
    integriteit: H
    vertrouwelijkheid: H
    mitigaties:
      beschikbaarheid:
        - maatregel: Een verbindingslimiet per project.
          bron: features/postgresql-connection-limits.md   # plan, feature-doc of RC-nummer
          toets: operations-manager/python/tests/test_connection_limit.py
      integriteit: [...]
      vertrouwelijkheid: [...]
```

`bron` is het plan, het feature-document of het RC-nummer dat de maatregel bouwt; `toets`
is de test of policy die hem bewijst, repo-relatief, en mag ontbreken. Een pad in `bron`
of `toets` dat niet meer bestaat is een gat.

Het BBN staat niet in het bestand: het wordt afgeleid uit de hoogste score (L geeft BBN1,
M geeft BBN2, H geeft BBN3) en in het overzicht gerenderd. Een opgeschreven kopie van een
afgeleide waarde is een tweede plek die kan gaan afwijken.

## Wat de controles afdwingen

De pydantic-modellen in `opi/tools/biv_render.py` weigeren een onvolledige entry:

- elke component heeft alle drie de assen en een unieke id;
- een score van M of hoger vraagt ten minste één mitigatie op die as;
- een onbekende as of een onbekend veld is een fout, geen stille aanname.

`task biv-check` (en de CI-job) faalt daarbovenop bij:

- een score H langs een as zonder enkele mitigatie met een toets;
- een genoemde toets of bron die niet meer bestaat;
- een component met score H die geen doel van de aanvalsproef is;
- een overzicht dat achterloopt op de inventaris.

De melding noemt het component en de as, zodat duidelijk is waar het gat zit.

## De aanvalsproef leest dezelfde lijst

`aanvalsproef_doelen()` geeft de component-id's die de aanvalsproef moet dekken. Het
harnas van de proef (zie `features/aanvalsproef.md`) leest die functie in plaats van een eigen
lijst van onderdelen bij te houden, en `ongedekte_aanvalsproef()` meldt elk component met
score H dat er niet in staat.

## Afhankelijkheden

`pydantic` en `pyyaml`, beide al afhankelijkheden van OPI. De renderer draait buiten de
app om en wordt nergens in de draaiende applicatie geimporteerd.

## Zie ook

- `plans/technische-review-bio-en-nora-bevindingen.md`: de BIO/NORA-review die naar deze
  inventaris verwijst in plaats van een eigen classificatie op te schrijven.
- `plans/bio2-compliance-analysis.md`: de BIO2-maatregelen per controldomein.
