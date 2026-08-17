# De diensten gaan mee naar het nieuwe thema

Status: plan, 10 augustus 2026. Aanleiding: de nieuwe vormgeving is de standaard, maar elk blok dat een *dienst* levert valt terug op de oude. Op de projectpagina zie je dat direct: het bijlagenblok komt binnen als `rvo-card` met `utrecht-button`, terwijl alles eromheen `nldd` is. De kaart om een bijlage verdwijnt daarbij, dus je ziet een kale regel.

## Wat er nu is, gemeten

| | |
|---|---|
| standaardweergave | de nieuwe (`DEFAULT_LAYOUT = LAYOUT_LOTC`) |
| dienstsjablonen via de oude renderer | **10** |
| `opi/services/catalog/` op het zoekpad van het nieuwe systeem | **nee** |
| componentenpakket gepind op | `85753c0`, terwijl master op `0132d13` staat |

De tien:

```
attachments/section-detail.html.j2        keycloak/section-detail.html.j2
invite/section-detail.html.j2             keycloak/otp-code.html.j2
metrics_scraper/section-deployment.html.j2
shared/section-backups.html.j2            shared/_backup-snapshots.html.j2
shared/_job-modal.html.j2                 shared/_backup-snapshots-one.html.j2
shared/_db-console-modal.html.j2
```

Dat raakt vijf pagina's en vier dialogen: bijlagen, uitnodigingen, keycloak, backups, metrics per deployment, plus de job-, database-console-, OTP- en snapshotdialoog.

**Waarom het niet vanzelf meegaat.** Twee componentsystemen kunnen niet in één Jinja-omgeving: de eerst geregistreerde voorbewerker eist elke `<c-*>`-tag op. Daarom staat `catalog/` bewust niet op het nieuwe zoekpad en rendert `render_roos()` die blokken in de oude omgeving, waarna de HTML wordt ingeplakt. Dat was een brug om geen functionaliteit te laten verdwijnen, met in de docstring de aantekening dat zo'n blok rvo-klassen draagt "totdat de dienst zelf meegaat".

## De beslissing is genomen: ROOS gaat eruit

Niet omzetten met een brug ernaast, maar **weghalen**. Er wordt overgestapt, niet parallel gedraaid, en zolang de oude weg bestaat blijft elke wijziging twee keer werk en gaan de twee uit de pas lopen. Dat is vandaag al gebeurd: een sjabloon dat door niemand meer gerenderd werd, een blok dat in het verkeerde thema binnenkwam, en tests die de oude pagina als maatstaf namen voor de nieuwe.

Wat dat betekent, gemeten:

| | |
|---|---|
| sjablonen in `opi/templates/` (roos) | **155** |
| sjablonen in `opi/templates_lotc/` | **212** |
| `roos="..."`-aanroepen in `render()` | **36** |
| `jinja-roos-components` in `pyproject.toml` | 1 afhankelijkheid |

Er is dus meer nieuw dan oud, en dat is het bewijs dat de overstap grotendeels achter ons ligt. Wat eruit moet: de 155 oude sjablonen, de schakelaar (`lotc_switch`: `chosen_layout`, `wants_lotc`, de dubbele `render(roos=..., lotc=...)`), `render_roos()`, de `jinja-roos-components`-afhankelijkheid en de statische roos-assets.

**Volgorde, want dit is geen sloopwerk maar een verhuizing.** Eerst de tien dienstsjablonen omzetten, want dat is het enige dat de nieuwe kant nog mist. Dan pas de oude kant weghalen, zodat er nooit een moment is waarop iets nergens meer staat. Een `roos=`-aanroep die je weghaalt terwijl er geen LOTC-sjabloon is, is een pagina die verdwijnt.

**Loop de 36 aanroepen langs voordat je ze schrapt.** Bij elke: bestaat het LOTC-sjabloon, en toont het hetzelfde? Vandaag bleek er een LOTC-sjabloon te bestaan dat door niemand gerenderd werd (`bg/project-details.html.j2`), en dat is precies het soort wees dat je bij een sloop over het hoofd ziet.

## Wat er moet gebeuren

1. **Het componentenpakket bijwerken** naar de huidige master (`0132d13`) en kijken wat dat oplevert: een component dat wij nabouwen en die inmiddels geleverd wordt, hoort te vervallen. Dat is eerder precies zo gegaan met `c-secret-field`.

2. **De tien sjablonen omzetten** naar het nieuwe dialect. De verschillen die telkens terugkomen:

   | oud | nieuw |
   |---|---|
   | `backgroundColor`, `justifyContent`, `showIcon` | kebab-case: `background-color`, `show-icon` |
   | `kind="secondary"` | `type="secondary"` |
   | `@click="..."` | `:attrs="{'onclick': ...}"` |
   | `class="rvo-text--sm"`, `style="display:flex"` | een component, of het weglaten |

   Kale themaklassen en inline styles horen er niet in terug te komen: dat is precies wat een themawissel breekt.

3. **`catalog/` op het zoekpad** van de nieuwe omgeving, en `render_roos()` weg.

3b. **ROOS eruit.** De 155 oude sjablonen, de schakelaar, de afhankelijkheid en de statische assets. Pas nadat stap 2 klaar is en stap 4 zegt dat de nieuwe kant compleet is.

4. **Alles nalopen, met beeld.** Dit is de kern van de opdracht en niet de afronding: er is het gevoel dat er van alles gemist is, en dat gevoel is terecht gebleken. Elke wizard, elke knop, elke actie, elke dialoog. Maak per scherm een schermafbeelding en kijk ernaar; een test die groen is zegt niet dat een pagina er goed uitziet, en dat is vandaag meermaals gebleken.

5. **De browsersuite uitbreiden waar hij dit niet zag.** Vandaag kwamen vijf dingen boven die de suite niet ving: een kaartenblok zonder tussenruimte, een knop die op geen enkel tabblad stond, een dienstblok in het verkeerde thema, een link naar een 404, en een deployment zonder repository. Zoek per geval uit waarom de suite zweeg en repareer dat, want anders is de volgende ronde hetzelfde.

## Waar op te letten

**Werken de knoppen nog.** De omzetting raakt `@click`, en dat is precies wat een knop doet. Een blok dat er goed uitziet maar niets meer doet is erger dan een blok dat er niet uitziet. Toets per blok dat de aanroep vertrekt en waarheen, zoals `test_lotc_project_tab.py` dat al doet.

**De klassen die geen vormgeving zijn.** `config-item`, `config-code`, `copy-btn`, `deployment-section`, `is-hidden`: daar hangt JavaScript aan. Die blijven staan, ook als ze er als opmaak uitzien.

**Meet in de browser, niet in de markup.** Een `gap` die als CSS-variabele wordt gezet, zegt niets tot je de berekende waarde opvraagt. Zo is vandaag gevonden dat de kaarten elkaar exact raakten terwijl het sjabloon klopte.

**Wat een dienst levert, blijft van de dienst.** De verleiding is om zo'n blok in de paginatemplate te trekken nu je er toch aan zit. Niet doen: dan is de volgende dienst weer handwerk, en de hele opzet van RC-36 was juist dat alles van een dienst in zijn eigen map staat.
