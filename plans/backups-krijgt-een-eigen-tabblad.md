# Backups krijgt een eigen tabblad

Status: plan, 13 augustus 2026. Gevraagd: Backups uit de deploymentpagina naar een eigen tabblad, met dezelfde structuur als Metrics.

## Wat er nu is

Het backupsblok komt niet uit een tabbladsjabloon maar uit een **generiek mechanisme**: `deployment_service_sections` in `bg/project-tabs.html.j2` (rond regel 725). Elke dienst levert daar zijn eigen blok per deployment, en het sjabloon noemt geen enkele dienstnaam. Backups is er daar één van; `opi/services/catalog/shared/backups.py:36` wijst naar `shared/section-backups.html.j2`.

Metrics werkt anders: dat is een echt tabblad met een eigen route en een deployment in het pad (`/projects/<project>/metrics/<deployment>`, RC-92).

## De keuze die eerst gemaakt moet worden

Een eigen tabblad voor Backups betekent **één dienst uitzonderen** op een mechanisme dat juist bedoeld is om dat niet te hoeven. Dat is te verdedigen, maar zeg welke van de twee je doet:

1. **Backups met naam noemen** in het tabbladsjabloon, zoals Metrics dat is. Simpel en direct, maar de volgende dienst die een eigen tabblad wil is opnieuw handwerk, en het sjabloon weet dan wel van diensten af.
2. **De dienst laat zelf weten dat zijn blok een eigen tabblad verdient**, via een haak zoals de andere dienstverklaringen. Dan is het generiek en werkt het ook voor de volgende. Meer werk, en het vraagt dat de tabbalk zijn tabbladen deels uit de dienstenregistry haalt.

Optie 2 past bij hoe de rest van dit systeem is opgebouwd (RC-36: alles van een dienst staat in zijn eigen map), maar optie 1 is verdedigbaar als er verder geen kandidaten zijn. Meet dat: **welke diensten leveren nu een deploymentsectie?** Is Backups de enige die groot genoeg is voor een eigen tabblad, dan is optie 1 eerlijk.

## Wat er hoe dan ook moet gelden

* **Dezelfde structuur als Metrics**: één deployment per pagina, met zijn naam in het pad (`/projects/<project>/backups/<deployment>`), een kiezer, en de keuze die meereist bij het wisselen van tabblad.
* **Beide adresvormen registreren**, letterlijk en niet als patroon, om dezelfde reden als bij de andere tabbladen: `/projects/{project}/{tab}` zou ook `/projects/details/<naam>` opvangen.
* **Het blok verdwijnt van de deploymentpagina.** Niet kopiëren; twee weergaven van dezelfde gegevens lopen uit de pas.
* **Let op het lui laden.** Het backupsblok haalt zichzelf op met `hx-trigger="intersect once"` en dat was een bewuste keuze: per deployment een verzoek opende evenzoveel Kopia-verbindingen. Met één deployment per pagina vervalt de reden om te verbergen, maar niet de reden om lui te laden.

## De toets

- `/projects/<project>/backups/<deployment>` toont de backups van die ene deployment;
- van Backups naar Deployments of Metrics wisselen houdt dezelfde deployment vast;
- het blok staat niet meer op de deploymentpagina;
- de oude adressen doen wat er besloten is;
- er staat opgeschreven welke van de twee opties gekozen is en waarom.

## Waar op te letten

**Niet en passant het backupsblok verbouwen.** Dit gaat over waar het staat, niet over wat erin staat.
