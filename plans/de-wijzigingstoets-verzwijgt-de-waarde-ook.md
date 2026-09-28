# De wijzigingstoets verzwijgt de waarde, net als de waardetoets ernaast

Status: plan, 17 september 2026. Niet gebouwd. Komt uit de SECURITY-review van RC-168 (PR #162).

**De aanname waaronder dit destijds niet-blokkerend was, is vervallen.** De beschrijving zei: "geen enkele dienst declareert vandaag een `ConfigSetting`, dus de wijzigingsregel draait nergens." Sinds RC-201 (gemerged 17 september 2026) is dat niet meer waar: `CONNECTION_LIMIT` in `opi/services/catalog/postgresql_database/connection_limit.py` is de eerste `ConfigSetting` van het platform, met `layers=(ConfigLayer.PROJECT, ConfigLayer.DEPLOYMENT)`. De wijzigingsregel loopt dus nu wel.

## Wat er is, gemeten

RC-168 heeft de weigering op een EIGENSCHAPSblok verhard. Die blokken (`user-env-vars`, `aliases`) dragen de eigen omgeving van een component, en `UserEnvVarsConfig` accepteert een platte `dict[str, str]`, dus een waarde die daar wordt gelezen kan een geplakt geheim zijn.

`_check_declared_settings` (`opi/manager/project_validation.py:143`) bouwt de zin daarom op uit de DECLARATIE in plaats van uit de waarde:

```python
reason = (
    f"'{e.setting.path}' valt buiten zijn speelruimte. {e.setting.latitude()}"
    if not name_value and e.setting is not None
    else str(e)
)
```

Dezelfde blokken gaan ook door de WIJZIGINGStoets, en die heeft die verharding niet. `validate_service_setting_changes` rendert de exceptie letterlijk:

```python
except SettingError as e:
    raise ProjectIntegrityError(
        f"Project '{project_name}': configuratie van service '{block.name}' kan niet zo worden gewijzigd: {e}"
    )
```

Het vervelende geval uit de meting: de NIEUWE versie is volledig geldig, dus de waardetoets kijkt er niet naar om. De waarde die uitlekt komt uit de VORIGE versie, en die wordt nergens opnieuw op zijn vorm beoordeeld. De zin gaat naar de aanroeper in het HTTP-antwoord en, op de elf `enforce_validation=False`-schrijvers, naar `logger.warning`.

## Wat er vandaag wel en niet bereikbaar is

Wees hier precies, want het bepaalt de urgentie en niet de oplossing.

- **Het mechanisme is bereikbaar.** Er is een `ConfigSetting`, dus de wijzigingstoets kan afgaan en de melding kan een vorige waarde bevatten.
- **Het gevoelige geval nog niet.** Dat vraagt een setting op een eigenschapsblok, en `CONNECTION_LIMIT` staat op `PROJECT` en `DEPLOYMENT` voor postgresql-database. Wat er vandaag hooguit uitlekt is een getal.

Dat is precies de reden om het nu te doen: de verharding aanbrengen terwijl er nog niemand op leunt is goedkoper dan hem er later tussen wurmen, en de asymmetrie tussen twee toetsen die naast elkaar staan is op zichzelf een val voor de volgende lezer.

## Wat er moet gebeuren

Geef de wijzigingstoets dezelfde behandeling als de waardetoets: bouw de zin op uit de declaratie zodra het blok een eigenschapsblok is.

**Het onderscheid hoort op het blok, niet op de aanroepplek.** Vandaag bepaalt de aanroeper het: `_check_declared_settings(..., name_value=False)` staat in de tak die eigenschapsblokken afhandelt. Zet je in de wijzigingstoets een tweede, eigen bepaling neer, dan zijn er twee plekken die moeten weten welke blokken gevoelig zijn, en die lopen uit elkaar zodra er een derde soort blok bijkomt.

Voorstel: laat `ServiceConfigBlock` zelf dragen of hij een eigenschapsblok is, gevuld op de plek waar de wandeling die blokken aanmaakt (`_owned_property_blocks`). Beide toetsen lezen dan hetzelfde veld. Eén bron, en een nieuw bloktype hoeft maar op één plek beoordeeld te worden.

Trek de opbouw van de zin uit tot één helper die beide toetsen gebruiken, zodat de formulering ook niet uit elkaar kan lopen.

## De toets

- een wijziging die door de wijzigingstoets geweigerd wordt op een EIGENSCHAPSblok noemt de waarde niet, maar het veld en zijn speelruimte, net als de waardetoets;
- dezelfde weigering op een blok in een `services:`-lijst noemt de waarde wél. Dat is bewust: daar is de grens zelf wat wordt teruggeciteerd, en dat staat zo in de docstring van `_check_declared_settings`;
- het geval uit de meting, waarin de nieuwe versie geldig is en de vorige de ongeldige waarde draagt, lekt niets meer. Dit is de kern: een test die alleen een ongeldige nieuwe waarde probeert, raakt het probleem niet;
- de melding gaat ook niet via `logger.warning` naar buiten op een `enforce_validation=False`-pad;
- de bestaande verharding in `_check_declared_settings` is ongewijzigd en zijn tests blijven groen;
- de twee toetsen delen de opbouw van de zin: `grep` op de formulering levert één plek op, geen twee.

## Waar op te letten

**Dit is een meldingswijziging, geen regelwijziging.** Wat geweigerd wordt verandert niet, alleen wat er in de zin staat. Een implementatie die per ongeluk ook de weigering zelf aanpast, verandert gedrag dat RC-168 bewust heeft neergezet.

**RC-172 is een aparte taak en blijft liggen.** Die gaat over per-mount records op de DEPLOYMENT_COMPONENT-laag, en daar declareert nog niets, ook na RC-201 niet. Los hem hier niet half mee op.
