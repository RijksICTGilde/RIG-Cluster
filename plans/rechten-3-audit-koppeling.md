# Rechten, taak 3: de audit aan het rechtensysteem koppelen

Opdracht: laat elke schrijfactie en elke weigering een rij in de audit opleveren, zonder code per route, en toon de audit per project. Dit verandert geen gedrag: wat mag en niet mag blijft gelijk.

**Ship dit pas als taak 1 (`plans/rechten-1-rechtensysteem.md`) en taak 2 (`plans/rechten-2-audit.md`) allebei op main staan.** Lees `plans/rechten-op-paden-tad-en-wies.md`, paragraaf 6 en 8.

## Waarom

Taak 1 laat bij elke beslissing de action, de resource, de beslissing en de step-up op `request.state` achter (ontwerp 8.3). Taak 2 levert `record_action` (ontwerp 8.2) en `list_actions`. Deze taak verbindt die twee, en voegt de twee dingen toe die alleen na een verzoek bekend zijn: de statuscode en de commit van het projectbestand.

De review en de security-gate van taak 2 lieten punten bij deze taak achter, omdat ze pas een gat worden zodra er een route of een tweede soort bij komt. Ze staan hieronder bij de stap waar ze horen, gemarkeerd met **(overdracht taak 2)**.

## Wat er moet gebeuren

### 1. De commit-SHA verzamelen

`ProjectStore.save` en de andere muterende methoden in `opi/services/project_store.py` geven een `MutationResult` met `ref` terug, de commit. Leg elke `ref` van een mutatie met `committed=True` vast in een contextvar per verzoek (zoals de actor in `opi/core/actor.py` een contextvar is). Een verzoek kan meer dan één commit maken; het is een lijst.

*Bewijs:* een test die binnen één verzoek twee keer opslaat en twee SHA's terugziet; een idempotente opslag (`committed=False`) voegt niets toe; een ander verzoek ziet ze niet.

### 2. De hook

**Eén verzoek, meer dan één beslissing.** Taak 1 (RC-392) neemt op sommige routes twee beslissingen: een upsert met `cloneFrom` weegt eerst het doel en daarna apart de bron (`Action.DEPLOYMENT_CLONE_FROM`, `opi/api/endpoint_util.py`). `record_decision` in `opi/core/authz/engine.py` overschrijft de vier attributen bij elke beslissing, dus de audit zou alleen de laatste zien. Laat `record_decision` daarom ook elke beslissing toevoegen aan een lijst `request.state.authz_decisions` (action, resource, beslissing, step-up). De vier losse attributen blijven, want `opi/core/auth_decorators.py` leest `authz_decision`. De middleware hieronder schrijft een rij per beslissing in die lijst; statuscode, commit-SHA's, taak-id en origin zijn per verzoek en staan op elke rij gelijk.

Eén middleware die na het antwoord `request.state.authz_decisions` leest. Is die er niet of leeg, dan gebeurt er niets. Anders schrijft de middleware per beslissing een rij als die een weigering is, of als de action geen leesactie is (de markering `read` uit het register van taak 1). De argumenten van `record_action`:

| argument | bron |
|---|---|
| `actor` | de actor van het verzoek; zie de **actor-kolom** hieronder |
| `action` | `str(action)` van de beslissing |
| `resource_path`, `project`, `deployment`, `environment` | de resource van de beslissing |
| `allowed`, `cause`, `grant` | de beslissing |
| `step_up` | de step-up van de beslissing |
| `status_code` | het antwoord |
| `commit_shas` | de contextvar uit stap 1 |
| `task_id` | de taak die het verzoek startte, als die er is; zoek waar `async_task_service` een taak aanmaakt en leg de id op dezelfde manier vast als de SHA |
| `origin` | `opi/utils/client_ip.py`, `client_ip(request)` |
| `fields` | de velden die de route in `velden=` van `@requires_step_up` noemt, met hun ingediende waarde; geen andere; zie **`fields`** hieronder |

Schrijf de rij zonder het antwoord op te houden: na het versturen, als achtergrondtaak, met een eigen databasesessie.

Waarden die niet in het projectbestand staan (welk image, welke back-up): kijk welke schrijfroutes dat hebben. Breid voor die routes `velden=` uit, of voeg naast `@action` een `audit_fields=(...)` toe als er geen step-up op zit. Noem in de PR welke routes dat zijn en waarom het bestaande niet volstond.

**De actor-kolom (overdracht taak 2).** Het gebeurtenissenontwerp (`features/futures/gebeurtenissen-vastleggen-en-melden.md`, de rij over BIO2 8.15.01) laat `actor` leeg op de projectsleutel, omdat die geen persoon droeg. Die sleutel bestaat niet meer: sinds schema 2.12 heeft een project tokens per rol, en elke ingang kent zijn actor. `record_action` vult `actor` daarom altijd, zoals taak 2 het al doet, ook voor een scheduler (de procesnaam is geen persoonsgegeven). Pas `record_action` hier niet op aan.

De oude projectsleutel staat nog als terugval in `recognise_principal` (`opi/api/principal.py`), voor een bestand dat de migratie naar 2.12 niet open kreeg. Die terugval valt buiten deze taak; hij schrijft `actor_kind` `project-api-key` met de projectnaam als identiteit, en dat is goed genoeg voor de audit.

**`fields` (overdracht taak 2).** De bewaarlus zet na 90 dagen alleen de kolommen `actor` en `origin` op `NULL`. Wat in `data.fields` staat blijft een jaar staan. Er mag dus nooit een persoonsgegeven in `fields`: geen e-mailadres, geen lidnaam, geen IP-adres, ook niet als waarde van een veld dat de route noemt. Een route waarvan een genoemd veld zo'n waarde kan dragen (de ledenroutes bijvoorbeeld) noemt dat veld niet; de commit-SHA wijst de wijziging dan aan. Breid de bewaarlus hiervoor niet uit: komt er toch een persoonsgegeven in `data`, dan is de fout de route, niet de lus.

*Bewijs:*

- een test die door **elke** route uit de routetabel loopt waarvan de action geen leesactie is, hem één keer toegestaan en één keer geweigerd aanroept, en per aanroep precies één rij ziet met de juiste actor, action, resource en uitkomst;
- een leesroute die toegestaan wordt levert geen rij; een leesroute die geweigerd wordt wel;
- een upsert met `cloneFrom` die toegestaan wordt levert twee rijen, een voor het doel (`deployment.write`) en een voor de bron (`deployment.clone-from`); wordt de bron geweigerd, dan staat die weigering als rij in de audit;
- een opslag in een projectbestand levert een rij met de SHA van die commit, en `git show` van die SHA in de test-repo bevat de wijziging;
- een gefaalde `record_action` laat het antwoord ongemoeid;
- een verzoek met een token van een gebruiker en een met een token van een service account leveren elk een rij met `actor` en `actor_kind` gevuld;
- een test die alle routes met `velden=` of `audit_fields=` langsloopt en faalt als er een veld bij zit uit een vaste lijst van velden die een persoon aanwijzen (leden, e-mail, gebruikersnaam); de lijst staat in de test.

### 3. Het audit-tabblad

Een tabblad op de projectpagina dat `list_actions` toont: tijd, wie, wat, waarop, uitkomst, en een link naar de commit als die er is. Achter `Action.AUDIT_READ`, met een regel in het register die het aan `admin` geeft. Platformbeheerders zien het op elk project. LOTC-componenten, net als de andere tabbladen in `opi/templates_lotc/bg/project-tabs.html.j2`. `origin` wordt niet getoond: die kolom is volgens het gebeurtenissenontwerp alleen voor platformbeheerders, en die lezen hem in de database, niet op de projectpagina.

**Autorisatie op `list_actions` (overdracht taak 2).** `list_actions(project=...)` controleert zelf niets: wie hem aanroept met een projectnaam krijgt de rijen van dat project. De route is daarom de enige guard. Het project komt uit het pad (`/projects/{project}/...`), gaat via `@action(Action.AUDIT_READ)` als `Resource` naar `decide()`, en de route geeft precies dat project door aan `list_actions`. Nooit een projectnaam uit de querystring of de body, want dan beslist `decide()` over het ene project en leest de route het andere.

**Een plafond op `limit` (overdracht taak 2).** `list_actions` neemt elke `limit`, ook een miljard. De route accepteert `limit` met een standaard van 50 en een maximum van 200, en geeft bij een grotere waarde 200 rijen terug, geen fout. Wil je bladeren, doe dat met een `before`-tijdstip, niet met een offset.

*Bewijs:* een admin ziet het tabblad en de rijen van zijn project; een developer ziet het tabblad niet en krijgt 403 op de route; een admin van project A krijgt 403 op de route van project B, ook als hij `?project=A` meegeeft; `limit=1000000` geeft hooguit 200 rijen; de pagina bevat geen `origin`; de matrixpagina toont de nieuwe regel.

### 4. De bewaarlus op de soort (overdracht taak 2)

`run_retention` in `opi/services/audit.py` werkt op alle rijen van `notification_events`, ongeacht `type`. Nu is de audit de enige schrijver, maar het gebeurtenissenontwerp geeft latere soorten een eigen termijn (beveiligingsgebeurtenissen houden de actor langer, en die termijn hangt aan de soort). Beperk de `DELETE` en de `UPDATE` in `run_retention` tot `ACTION_TYPES`, zodat een volgende soort niet stil met de termijn van de audit wordt gewist. Controleer met `EXPLAIN` dat de `UPDATE` de partiele index `idx_notification_events_actor_sweep` nog gebruikt; zo niet, noem het in de PR, verander de index niet in deze taak.

*Bewijs:* een rij van een andere soort, ouder dan een jaar en met een actor, staat er na een run nog, met zijn actor; een auditrij van dezelfde leeftijd is weg.

### 5. Documentatie

Werk `features/audit.md` uit taak 2 bij: de koppeling, de velden, het tabblad, dat `actor` altijd gevuld is, de regel dat `fields` geen persoonsgegeven draagt, en dat de bewaarlus alleen de auditsoorten raakt.

## Wat er buiten valt

- Wat er mag: de regels uit taak 1 veranderen niet, behalve de nieuwe regel voor `audit.read`.
- De tabel: taak 2. De bewaarlus ook, op de beperking tot `ACTION_TYPES` in stap 4 na.

## Wat niet mag

- Geen OPI uitrollen.
- Geen persoonsgegeven in `data`, `summary` of `subject`.
- Geen projectnaam uit de querystring of de body voor `list_actions`.

## Verifieerbaar

1. `uv run ruff check . --fix && uv run ruff format . && uv run pyright` schoon in `operations-manager/python`.
2. De routetabeltest uit stap 2: elke schrijfroute, toegestaan en geweigerd, één rij.
3. De overdrachtspunten elk met hun test: `actor` gevuld per tokensoort, geen persoonsveld in `fields`, 403 op een ander project, het plafond op `limit`, de bewaarlus per soort.
4. De bestaande suite groen, inclusief de matrixtest uit taak 1.
