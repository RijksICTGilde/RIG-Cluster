# TODO_FUTURE

Punten die we bewust later doen. Ze staan hier zodat `TODO.md` gaat over wat we nu oppakken,
en niet omdat ze onbelangrijk zijn. Verhuizen terug zodra we ze wel willen doen.

- [ ] Volledige scan van taalgebruik en één keuze maken: overal Nederlands, of de vertaallaag echt in gebruik nemen. De infrastructuur staat er al en is bedraad, maar wordt niet gebruikt: `opi/locale/` heeft `base.pot` plus `en_US` en `nl_NL` met `.po` en `.mo`, en `core/templates.py:233` installeert de gettext-vertalingen in de Jinja-omgeving via `core/i18n.py` (babel). Alleen is de catalogus leeg, `nl_NL/LC_MESSAGES/messages.po` bevat één `msgid` en dat is de header, terwijl minstens vijftien templatebestanden hardgecodeerde Nederlandse teksten dragen. Er loopt bovendien een derde, parallel mechanisme: invites hebben per invite eigen `nl`/`en`-blokken via `$defs/i18n-text` in `project_v2.json`. Te beslissen: (a) Nederlands als enige taal en dan de gettext-plumbing weghalen in plaats van hem leeg te laten staan, of (b) strings markeren en de catalogus vullen, en dan ook bepalen wat er met die per-invite i18n gebeurt. Wat het ook wordt, de huidige toestand is de slechtste van de drie, want hij suggereert vertaalbaarheid die er niet is.

Gemeten op 6 augustus, en dat maakt de keuze makkelijker: de vertaallaag is volledig
geinstalleerd (`install_gettext_translations` in `opi/core/templates.py`) en wordt door
NUL van de 160 sjablonen gebruikt. Het is dus geen half werk maar dood gewicht met een
beslissing eromheen.
