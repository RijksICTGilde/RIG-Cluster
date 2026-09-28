# LOTC-pin van 762e570 naar 172300a: NLDD 0.8.80 naar 0.8.92

De componentenlaag (lord-of-the-components, LOTC) is bijgewerkt en de operations-manager pint een commit van 12 augustus. Dit is geen bump die je in het voorbijgaan doet: er zitten vijf NLDD-releases in, drie wijzigingen raken markup waar deze applicatie zelf op selecteert, en de compiler is strenger geworden. Alles hieronder is gemeten op forgejo/main van RIG-Cluster en op de tip van LOTC (forgejo/master, 172300a), niet uit commitberichten gelezen.

## Wat er gemeten is

**De pin.** `operations-manager/python/pyproject.toml` pint alle vijf packages (`lord-of-the-components`, `lotc-rvo`, `lotc-nldd`, `lotc-layout`, `lotc-forms`) op `762e57090ea32bd6f1c3d717f2fa6c9ac1ef2f4e`, NLDD 0.8.80. Die commit staat op GitHub alleen als branch `ci-pin-762e570`, niet op master; zijn 267 commits zitten patch-gelijk allemaal in de LOTC-master. Het verschil tussen ons pin en de tip is 31 commits, van 14 augustus tot 26 september 2026.

**De upgrade-notitie van LOTC** staat in `docs/UPGRADEN-VANAF-d19c7a5.md` van dat repo en is geschreven vanaf d19c7a5 (0.8.83, 20 augustus). Ons pin ligt daar VOOR. Wat de notitie beschrijft geldt dus allemaal voor ons, plus wat er tussen 14 en 20 augustus gebeurde: NLDD 0.8.80 naar 0.8.83 (`list-item-action` verwijderd, CSS-bundel geherstructureerd), `secret-field` als custom element met CSS en JS als losse bestanden (698e20c), strengere escaping van attribuutwaarden (46f2653, 7148906) en `_generic_attributes.j2` dat geen witregels per attribuut meer uitstoot.

**Waar de nieuwe pin vandaan moet komen.** GitHub `RijksICTGilde/lord-of-the-components` master staat op d19c7a5. Commit 172300a staat alleen op de Forgejo van dit team (anoniem te fetchen, gemeten met `git ls-remote`). Het pin in pyproject wijst naar GitHub, en dat moet zo blijven: een productie-image mag niet van een persoonlijke server afhangen. Daarom is stap 0 een harde controle.

**Wat wij zelf om LOTC heen gebouwd hebben en wat daarvan vervalt.**

- `opi/templates_lotc/components/_forms.j2` is ONZE KOPIE van de lotc-forms macro's, met drie wijzigingen (staat in de kop van het bestand). Wijziging 1 is de foutbedrading via het filter `foutbedrading` uit `opi/forms/lotc_attrs.py` (`bedraad_foutmelding`): het schrijft `invalid`, `aria-invalid` en `error-message="<id>"` op de control en emitteert `<nldd-form-field-error-text>`. NLDD 0.8.84 heeft dat element verwijderd en lotc-forms bedraadt de fout nu zelf goed: de veldsjablonen zetten `invalid unmet="<id>"` op de control en de macro emitteert `<nldd-validation-list><nldd-validation-item id="<id>">`. Wijziging 1 vervalt dus volledig. Wijzigingen 2 en 3 (geen "Optioneel"-badge bij `data-no-optional-badge`, en nooit bij een keuzelijst) blijven nodig: upstream kent `no-optional-badge` niet (0 treffers op de tip).
- `tests/test_lotc_foutmelding_veld.py` legt onze kopie naast de geinstalleerde en faalt op elk ander verschil. Die test is de detector voor deze stap en blijft bestaan, met nieuwe asserties.
- `tests/e2e/test_lotc_veldfout_zichtbaar.py` meet in een browser dat een foutregel hoogte heeft. Het selecteert op `nldd-form-field-error-text`; dat wordt `nldd-validation-item`. Dezelfde tagnaam staat in `tests/e2e/helpers/wizard.py` (FIELD_ERRORS), `tests/e2e/test_lotc_visual.py:187` en `tests/e2e/test_lotc_modal_dialoog.py:163`.

**secret-field.** Geen CSS van ons op `.lotc-secret` (0 treffers in `static/`). Wel selectors: `tests/e2e/test_componentkaart_uitklap.py:104` (`.lotc-secret__btn`), `tests/e2e/test_lotc_project_tab.py:225` (`.lotc-secret__btn[data-act='copy']`), `tests/e2e/helpers/sandbox_api.py:41,56` (`.lotc-secret__value`) en `scripts/sandbox_project_tool.py:79` (regex op `lotc-secret__value ... data-value=`). De waarde staat nu als `value` op het host-element `<lotc-secret-field>`, de knop draagt `data-action`.

**list-item-action.** `opi/templates_lotc/bg/project-tabs.html.j2:570` gebruikt `<c-list-item-action>` (het vraagteken bij een service) en `static/js/uitklap.js:27` sluit `nldd-list-item-action` uit van de rij-klik. Het heet nu `list-item-segment`, met dezelfde attributen.

**`default` als waarde, `modeless`:** 0 treffers. Niets te doen.

**Assets.** `<c-page>` bedraadt de `<head>` zelf uit `css_urls`/`js_urls` van elk actief design system, dus de vijf nieuwe bestanden komen automatisch mee. Twee dingen moeten wel nagemeten: de route `/static/lotc/{rel}` in `opi/server.py:690` valt terug op een lijst filesystem-roots in `opi/core/templates_lotc.py:158`, en `lotc-nldd.css`/`lotc-nldd.js` staan in het package BUITEN `dist/`; en `tests/e2e/test_sandbox_lotc.py` somt de assets op die in de image moeten zitten (`nldd/dist/nldd.js`, `layout/layout.css`, `app-components.css`), die lijst hoort de nieuwe bestanden erbij te krijgen.

**Strengere compiler.** `LOTC_STRICT` staat nergens in RIG-Cluster (0 treffers, ook niet in `.github/workflows/ci.yml`). `setup_components` in `opi/core/templates_lotc.py:81` wordt zonder strictness-argumenten aangeroepen. T-shirtmaten op `gap`/`padding` komen 506 keer voor buiten `c-layout-flow`, maar bijna alles is `c-card` (153, en `card.html.j2` vertaalt `md` zelf naar `20`) en de layout-componenten `c-stack`/`c-cluster`/`c-grid`/`c-auto-grid`/`c-columns`/`c-switcher` uit `lotc-layout`, die geen gegenereerde NLDD-renderers zijn. De ene `c-container` gebruikt al `padding="24"`. Wat er onder `LOTC_STRICT=1` werkelijk omvalt is dus een meting, geen aanname; verwacht klein.

**Escaping.** Onze `attr_escape` (`lotc_attrs.py`) en het `forceescape`-patroon uit `tests/test_lotc_attribuutwaarden.py` bestaan omdat LOTC `:attrs`-waarden met `| e` escapete en Markup ongemoeid liet. Upstream escapet sinds 46f2653 en 7148906 strenger. Dat kan dubbele escaping geven (`&amp;amp;`) op waarden die bij ons al langs `attr_escape` of `forceescape` gaan.

## Wat er moet gebeuren

0. **Controleer dat GitHub de commit heeft** voordat je iets anders doet: `git ls-remote https://github.com/RijksICTGilde/lord-of-the-components.git master` moet `172300af100ada5e747f340d0944342bd33a338e` geven. Zo niet: stop en meld het, niet uitwijken naar een andere URL.
1. **Verzet het pin** in alle vijf regels van `pyproject.toml` naar `172300af100ada5e747f340d0944342bd33a338e` en draai `uv lock`. Pas het commentaar erboven aan als het niet meer klopt. Verifieer: `uv sync --all-groups` slaagt en `lotc_nldd` meldt `nldd_version` 0.8.92.
2. **Zet de foutbedrading om, in dezelfde commit als het pin.** Neem `components/_forms.j2` opnieuw over van de geinstalleerde lotc-forms en breng alleen wijzigingen 2 en 3 uit de kop opnieuw aan; schrijf de kop bij (wijziging 1 is er niet meer, en de zin "weghalen zodra lotc-forms zelf ... schrijft" is nu waar). Verwijder `bedraad_foutmelding`, `_FOUT_IDS`, `_EERSTE_TAG`, `_INVALID` uit `lotc_attrs.py` en de filterregistratie in `templates_lotc.py`; `attr_escape` en `field_attrs` blijven. Verifieer: `grep -rn "error-message-ids\|form-field-error-text\|foutbedrading\|bedraad_foutmelding" opi static tests scripts` geeft 0 treffers; `test_lotc_foutmelding_veld.py` bewaakt nu dat de control `invalid` en `unmet="<id>-error"` draagt, het item dat id heeft en `judging` NIET in de markup staat; de losse checkbox (eigen tak in `checkbox-field.html.j2`) zit in die test.
3. **Compileer alles onder `LOTC_STRICT=1`.** Zet `LOTC_STRICT=1` in de pytest-stap van `.github/workflows/ci.yml` en in de test-omgeving (pyproject `[tool.pytest.ini_options]` env, of conftest), NIET in de productie-image: een onbekende waarde moet in CI rood zijn en in productie niet een 500 opleveren. Draai de tests die alle sjablonen renderen (`tests/test_lotc_component_names.py`, `test_lotc_geen_rvo_resten.py`, `test_template_structure.py` en de rest die `rglob("*.j2")` doet). Los elke `Unknown attribute` en elke enum-fout op door de aanroep te repareren, niet door de controle uit te zetten. Noem in de PR wat er omviel; dat waren tot nu toe stille no-ops.
4. **Selectors van secret-field**: de vier bestanden hierboven. `scripts/sandbox_project_tool.py` leest de waarde voortaan van `<lotc-secret-field value="...">`. Verifieer met `tests/e2e/test_lotc_project_tab.py` (de kopieerknop) en `test_componentkaart_uitklap.py`.
5. **list-item-action naar list-item-segment** op de twee plekken. Verifieer in de browser dat het vraagteken bij een service de uitleg opent EN de rij niet omklapt; dat laatste is de reden dat `uitklap.js` het element uitsluit.
6. **Assets**: meet dat `/static/lotc/nldd/lotc-nldd.css`, `/static/lotc/nldd/lotc-nldd.js` en `/static/lotc/forms/forms.js` via onze route 200 geven (de roots in `templates_lotc.py:158`), en dat `<c-page>` ze in de `<head>` zet. Voeg ze toe aan `STATIC_ASSETS` in `tests/e2e/test_sandbox_lotc.py`. Controleer dat `nldd/dist/nldd.js` nog bestaat na de bundelherstructurering van 0.8.83.
7. **Escaping meten**: render een veld met een `:attrs`-waarde die `&`, `"` en `<` bevat en een `onclick` met JSON (het geval uit `test_lotc_attribuutwaarden.py`), en toets dat de HTML EEN keer geescapet is. Blijkt `attr_escape` of `forceescape` nu dubbel te werken, haal dan onze laag weg en niet die van LOTC, en zeg dat in de PR.
8. **Sweep**: `uv run python -m lord_of_the_components.sweep --design-systems nldd,lotc-forms opi/templates_lotc opi/services/catalog` en loop de lijst met de hand na: elke computed waarde op een enum- of icoonattribuut en elke `:attrs`-spread. Zet de uitkomst als korte lijst in de PR (wat is nagekeken, wat is aangepast).
9. **Meet in een browser dat een veldfout zichtbaar is.** `tests/e2e/test_lotc_veldfout_zichtbaar.py` met de nieuwe selector moet groen zijn op hoogte (> 8 px) en op de aria-bedrading; niet op de markup alleen. De markup kan er goed uitzien terwijl het element een leeg vak tekent, dat is precies hoe de vorige versie stuk was.
10. **Documentatie**: `features/lotc-bouwlijn.md` waar het over de foutbedrading of het pin gaat, en `request_for_components.md` als daar het verzoek om de foutbedrading nog open staat (dat is nu opgelost aan hun kant).

## Wat er buiten valt

- De omwegen uit sectie 3 van de upgrade-notitie opruimen: de vier `:attrs`-spreads die `class` doorgeven, en de raw tags voor structuurcomponenten. Dat is een aparte, mechanische veegactie.
- Het `:root`-blok met `--nldd-color-*` in `static/css/lotc-app.css` (LOTC ronde 9 zegt dat het weg kan). Werkt nog; niet aanraken in deze taak.
- `lotc-rvo` verwijderen als package. Blijft geinstalleerd, `tests/test_css_dode_variabelen.py` leest er nog uit.
- Het Rijkswapen dat LOTC nu meelevert gebruiken. Of dat mag is een rijkshuisstijlvraag, niet een technische.
- Nieuwe schrijfwegen: deze taak opent er geen. Het is markup, tests en een pin; de escaping wordt strenger, niet losser (stap 7 toetst dat).

## Verifieerbaar

- `uv run pytest tests/ -q -m "not slow and not e2e and not requires_infra and not sandbox"` groen met `LOTC_STRICT=1`, plus `ruff check`, `ruff format --check` en `pyright` schoon.
- `uv run pytest -m e2e -q` (standalone Playwright, geen sandbox nodig) groen, met in de PR de uitkomst van `test_lotc_veldfout_zichtbaar.py`, `test_lotc_project_tab.py` en `test_componentkaart_uitklap.py` apart genoemd.
- 0 treffers op `error-message-ids`, `form-field-error-text`, `lotc-secret__`, `data-act=`, `list-item-action` in `opi/`, `static/`, `tests/` en `scripts/`.
- De PR bevat: wat er onder `LOTC_STRICT=1` omviel, de sweep-lijst, en de escaping-meting uit stap 7.
