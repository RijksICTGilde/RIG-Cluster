# Kan LOTC naast jinja-roos-components bestaan?

Gemeten op 8 augustus 2026, tegen `lord-of-the-components` op branch
`plan-v7-lotc-thema-agnostische-compiler-performanc` (commit `26ab110`) en tegen de
`jinja-roos-components` die dit project vandaag gebruikt (`table-and-more-conversion`,
`d6ef4eb`). Na te doen met `scripts/lotc-coexistence-probe.py`.

Dit is de vraag die het componentenplan (`plans/naar-het-nieuwe-componentensysteem.md`)
open liet staan, en hij bepaalt de vorm van de hele proef: **kan de omzetting pagina voor
pagina, of is de kleinste eerste stap meteen de hele applicatie?**

## Het antwoord in een zin

Ze kunnen **niet in dezelfde Jinja-omgeving**, maar wel **in twee losse omgevingen naast
elkaar in dezelfde applicatie**. Pagina voor pagina kan dus, met als eenheid niet de
pagina maar de hele overervingsketen: een LOTC-pagina heeft zijn eigen `base`.

## Waarom niet in een omgeving

Beide systemen doen precies hetzelfde ding: ze registreren een Jinja-extensie die de
bron voorbewerkt en elke `<c-*>`-tag omzet, en ze hangen hun componenttemplates aan
`loader.searchpath`. Zet je ze allebei aan, dan lopen twee voorbewerkers over dezelfde
tags. De eerste die draait, claimt ze allemaal - en breekt hard op de tags die hij niet
kent.

De volgorde is de registratievolgorde, dus wie je als tweede aanzet, verliest:

| model | tag die alleen roos kent | tag die alleen LOTC kent | tag die beide kennen |
|---|---|---|---|
| A: eerst LOTC, dan roos | **faalt** | rendert (LOTC) | rendert **als LOTC/NLDD** |
| B: eerst roos, dan LOTC | rendert (roos) | **faalt** | rendert **als roos** |

De laatste kolom is het venijn: een tag die beide kennen (`c-heading`) rendert stilzwijgend
door het systeem dat toevallig eerst staat. Er is geen per-tag verdeling; het is alles of
niets, en welke van de twee het wordt hangt aan een regel volgorde in `templates.py`.

Een keten - de een laat de tags van de ander met rust en geeft ze door - bestaat niet.
Geen van beide systemen heeft een doorlaatstand. Alle vier de combinaties zijn gemeten:

| keten | uitkomst |
|---|---|
| LOTC (`on_missing_component="placeholder"`) → roos (strikt) | faalt |
| LOTC (`on_missing_component="placeholder"`) → roos (niet-strikt) | faalt |
| roos (`strict_validation=False`) → LOTC | faalt |
| roos (`strict_validation=False`) → LOTC (`placeholder`) | faalt |

`on_missing_component="placeholder"` helpt hier niet: die stand geldt voor een component
dat LOTC wel *kent* maar dat het actieve thema niet implementeert, niet voor een tag die
niet in de registry staat. Een vreemde tag is en blijft een harde fout. Hetzelfde geldt
voor `strict_validation=False` aan de roos-kant: dat versoepelt de attribuutcontrole, niet
de vraag of het component bestaat.

## Wat wel kan: twee omgevingen

Twee losse `Environment`-objecten, elk met een systeem, werken zoals verwacht: elk rendert
zijn eigen woordenschat en faalt alleen op die van de ander. Een applicatie kan die
allebei hebben en per route kiezen welke een pagina rendert.

De prijs is de grens. Die loopt niet per pagina maar per **overervingsketen**: een pagina
die `base.html.j2` uitbreidt, wordt door dezelfde omgeving gerenderd als die `base`. Wil
je een pagina op LOTC zetten, dan heeft die pagina een eigen LOTC-`base` nodig. Twee
schillen naast elkaar dus, tijdelijk, tot de laatste pagina om is.

Dat is goedkoper dan het klinkt, want de schil is klein. `base.html.j2` gebruikt zes
componenten, en vijf daarvan heten in LOTC hetzelfde:

| in `base.html.j2` | in LOTC |
|---|---|
| `c-page` | `page` |
| `c-header` | `header` |
| `c-footer` | `footer` (of `site-footer`) |
| `c-heading` | `heading` |
| `c-layout-flow` (2x) | `layout-flow` |
| `c-menubar` | `menu` - de enige hernoeming |

## De echte blokkade zit ergens anders

LOTC is vandaag **niet installeerbaar** voor dit project. Het staat alleen op de interne
Forgejo (`http://localhost:3000/robbert/lord-of-the-components.git`); er is geen publieke
of intern bereikbare locatie zoals `jinja-roos-components` die heeft
(`git+https://github.com/RijksICTGilde/jinja-roos-components.git`). Een dependency op
`localhost:3000` maakt de OPI-image alleen op deze ontwikkelmachine bouwbaar, en dat is
precies de destabilisatie die de werkafspraak ("de release gaat voor") uitsluit.

**Dat is de eerstvolgende stap, en het is geen werk in deze repo:** LOTC moet ergens staan
waar de bouw erbij kan. Zolang dat niet zo is, kan de navigatieproef niet draaien, hoe
klein hij verder ook is.

## Wat dit betekent voor de fasering

- **Fase 2** (de hernoemingen) hangt hier niet aan en is gedaan voor zover hij veilig kan:
  `c-p` is `c-paragraph` geworden. `c-menubar` naar `c-menu` kan nog niet, want roos kent
  geen `menu`; `tests/test_lotc_component_names.py` faalt zodra dat verandert.
- **Fase 3b** (de navigatieproef) kan qua vorm: twee omgevingen, een tweede `base`, en de
  proef blijft beperkt tot de navigatie. Hij kan nog niet qua bouw, zolang LOTC niet
  installeerbaar is.
- **Fase 4** (pagina voor pagina) blijft mogelijk. De grens is de overervingsketen, niet de
  losse pagina; plan de omzetting dus per schil-plus-pagina's, niet per template.
- **Fase 5** (de schil) verandert van karakter. Het is niet langer "als laatste `base`
  omzetten" maar "de tweede schil weghalen zodra de laatste pagina om is".

## Meting nadoen

```bash
git clone http://localhost:3000/robbert/lord-of-the-components.git lotc
cd lotc && git checkout plan-v7-lotc-thema-agnostische-compiler-performanc
uv venv probe-venv --python 3.14
VIRTUAL_ENV=$PWD/probe-venv uv pip install -e python -e packages/lotc-rvo \
    -e packages/lotc-nldd -e packages/lotc-layout -e packages/lotc-forms \
    beautifulsoup4 lxml
# jinja-roos-components erbij zetten (de git-dependency van dit project)
./probe-venv/bin/python /workspace/scripts/lotc-coexistence-probe.py
```
