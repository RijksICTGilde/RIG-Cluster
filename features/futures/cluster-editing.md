# Cluster editing (future feature)

Status: **niet ondersteund**. `clusters` op een bestaand project is op dit
moment niet wijzigbaar; de keuze wordt gemaakt bij projectaanmaak en blijft
daarna staan.

Beperkingen vandaag:

- Geen edit-mode form exposes het veld (`IDENTITY_EDIT_SECTION` in
  `forms/visualizers/wizard_sections.py` toont alleen display-name +
  description).
- Geen add/remove-logica voor cluster aan een bestaand project.
- `clusters` zit in `PROTECTED_PROJECT_KEYS`
  (`opi/web/project_edit_security.py`) als defense-in-depth: een form die
  het toch zou submitten wordt stilzwijgend genegeerd.

Bij implementatie: form-veld toevoegen, `clusters` uit
`PROTECTED_PROJECT_KEYS` halen, en de master-OPI propagatie testen voor
zowel add (nieuwe cluster pickt project op) als remove (vertrekkende OPI
ruimt op).
