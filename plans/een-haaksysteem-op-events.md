# Eén haaksysteem, op events

Status: plan, 6 augustus 2026. Niet gebouwd. Aanleiding: elke nieuwe uitbreiding vraagt nu een nieuwe methode op de basisklasse plus een nieuwe plek die de registry scant. Het vermoeden was dat dat uit de hand loopt; de meting bevestigt dat.

## De meting

```
29 publieke methoden op de Service-basisklasse
38 plekken in de code die over diensten itereren
 2 leden in de HookPoint-enum
```

Van de negenentwintig uitbreidingspunten lopen er dus **twee** via de enum (`AFTER_SYNC`, `DEPLOYMENT_STATE`). De rest is een methode met een eigen naam, die generieke code bij naam aanroept op een plek die daarvoor bedacht is.

Hoeveel diensten elke haak echt gebruikt, want dat scheidt contract van maatwerk:

| Haak | Diensten |
|---|---|
| `config_editables` | 17 |
| `config_api_fields` | 12 |
| `config_form_section` | 10 |
| `config_component_layout` / `..._visualizers` | 8 |
| `build_secret_files`, `provision` | 4 |
| `config_model_for`, `contribute_manifest_context`, `detail_page_sections` | 3 |
| `deployment_state` | 2 |
| `config_approvals`, `contribute_deployment_manifests`, `deployment_page_sections`, `observe_deployment`, `config_deployment_component_layout`, `..._visualizers` | 1 |

Zes haken hebben één bewoner. Dat zijn geen contracten maar maatwerk dat op de basisklasse is beland, en ze kosten wel iedereen die de klasse leest aandacht.

## Wat we willen

Een dienst luistert op een event uit een enum, en generieke code publiceert dat event. Geen nieuwe methode per uitbreiding, geen nieuwe scanplek. Hetzelfde geldt voor de UI: een dienst haakt op dezelfde manier in op waar hij zichtbaar is.

## De spanning die eerst beslecht moet worden

Dit is geen kwestie van doorvoeren, want er zit een echte afweging in.

De haken van vandaag zijn **getypeerd en vindbaar**: `detail_page_sections(project_data, user_role)` zegt wat het krijgt en wat het teruggeeft, pyright controleert het, en je vindt de gebruikers met één zoekopdracht. Een generieke event-bus met `handle(event, payload)` is uniform maar verliest precies dat: de payload wordt een dict, de typecontrole valt weg, en "wie luistert hierop" is niet meer te zien zonder te draaien.

Voor een codebase waar we vandaag drie keer een fout vonden doordat een test of een type iets vasthield, is dat geen kleine prijs.

De uitweg is waarschijnlijk niet kiezen maar splitsen: **de enum wordt de index en het dispatch-mechanisme, de contracten blijven getypeerd.** Eén plek die weet welke events er zijn en wie erop zit, terwijl een handler nog steeds een getypeerde methode is. Dan verdwijnen de 38 scanplekken en blijft de vindbaarheid.

Beslis dat expliciet voordat er iets verbouwd wordt, en schrijf de reden op.

## Voorstel

1. **Inventariseer per haak wat hij is**: een contract met meerdere bewoners, of maatwerk met één. De zes met één bewoner horen waarschijnlijk niet op de basisklasse.
2. **Beslis de vorm** (de spanning hierboven), en leg hem vast in `instructions/services.md`.
3. **Eén dispatch-punt** met de enum als index, waar de 38 scanplekken naartoe verhuizen. Verifiëren: het aantal plekken dat zelf over `SERVICES` itereert daalt aantoonbaar.
4. **De UI-kant meenemen**, want dat is dezelfde vraag: `detail_page_sections`, `deployment_page_sections` en de acties zijn allemaal "waar ben ik zichtbaar", en horen dus op dezelfde manier in te haken.

## Volgorde

1. De inventarisatie, want die bepaalt of dit een grote of een middelgrote klus is.
2. De vormbeslissing, met de argumenten uit dit plan erbij.
3. Het dispatch-punt met twee bestaande haken als eerste bewoners, gedrag ongewijzigd.
4. De rest in groepen, met na elke groep dezelfde verificatie.

## Waar op te letten

**Dit is geen opruiming maar een verbouwing van het contract.** Alles in `catalog/` hangt eraan. Doe het niet in één klap en houd na elke groep de suite groen, anders is een fout niet meer te herleiden naar een stap.

**Uniformiteit is geen doel op zich.** De 17 diensten op `config_editables` werken prima; dat is een contract dat zijn werk doet. De winst zit in de 38 scanplekken en de zes eenmalige haken, niet in het gelijkschakelen van wat al goed loopt.

**Een hook die het projectbestand muteert committeert niet zelf.** Dat contract staat al in `plans/oom-auto-tune-deployment-scoped.md` en geldt straks voor meer haken tegelijk: muteer `ctx.project_data`, en de aanroeper doet er één `save_and_commit_project()` overheen. Twee diensten die allebei committen geven een lost-update-race.

**Doe dit na RC-36.** Dat plan verhuist de servicedefinitie naar het pakket en raakt dezelfde bestanden. Twee verbouwingen tegelijk op `services.py` en `catalog/` levert een merge op die niemand meer kan nakijken.
