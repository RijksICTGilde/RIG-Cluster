# NLDD app-shell

## Wat het is

De paginaschil van de LOTC-bouwlijn (`opi/templates_lotc/base_lotc.html.j2`) gebruikt de layoutcomponenten van NLDD zelf, in dezelfde opbouw als de Fundament-console. De zijbalk was `<c-sidenav>`, een nabouw uit LOTC die afweek van het design system; die is vervangen door `nldd-list type="navigation"`.

```
nldd-shell
└ nldd-bar-split-view
  ├ pane slot=header  → nldd-top-navigation-bar (logo, Weergave, Beheer, account)
  └ pane slot=main
    └ nldd-navigation-split-view
      ├ pane slot=sidebar → nldd-page > nldd-simple-section > per groep een nldd-list
      └ pane slot=main    → nldd-page#hoofdpagina
                              ├ nldd-top-title-bar (alleen "Menu" op een smal scherm)
                              ├ nldd-simple-section > #hoofdinhoud > {% block content %}
                              └ voettekst: versie, Over het platform, Beveiliging
```

De menustructuur komt ongewijzigd uit `opi/web/navigation_lotc.py`, met onze iconen via `to_nldd_icon`. De inhoud van pagina's (tabs, kaarten, formulieren) is niet veranderd.

De voettekst is geen `nldd-page-footer`: die brede band hoort bij een contentsite en staat in een paneel van de app-shell los van de rest. Het is een smalle regel met alleen wat de zijbalk niet al heeft: de versie (met link naar de commit), Over het platform en Beveiliging.

## Gedrag

- **Breed scherm:** zijbalk en pagina staan naast elkaar en scrollen elk apart.
- **Smal scherm:** NLDD toont alleen de pagina. "Menu" linksboven haalt `has-content` van het hoofdpaneel (`static/js/zijbalk-terug.js`), waarna de split view zelf de zijbalk toont. Een klik in de zijbalk laadt de volgende pagina.
- **Scrollherstel na een htmx-swap** (`static/js/htmx-formgedrag.js`) leest en zet de scrollpositie van `#hoofdpagina`, of van het venster als NLDD in een kolom staat (`data-scroll="root"`).

## Een eigen zijbalk

Een pagina vervangt de zijbalk via `{% block sidenav %}`. Het voorbeeld staat in `opi/templates_lotc/bg/project-context.html.j2`: per groep een `nldd-title` en een `nldd-list type="navigation"` met `nldd-list-item href=... current`.

## Afhankelijkheden

- De NLDD-bundel uit `lotc-nldd` (`@nldd/design-system` 0.13.0). Het root-element heet daar `nldd-shell`; oudere documentatie noemt het `nldd-app-view`.
- `<c-page>` blijft de documentwrapper: die bedraadt de `<head>` en de assets en tekent zelf niets.
