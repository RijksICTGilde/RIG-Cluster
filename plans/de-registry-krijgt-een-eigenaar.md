# De registry krijgt een eigenaar

Status: plan, 13 augustus 2026. Bevinding A uit de technische review (`plans/technische-review-bio-en-nora-bevindingen.md`), gewogen als **kritiek** en als enige met een werkend end-to-end pad tussen tenants. Dit is een blokkade voor de uitrol.

## Wat er is, gemeten

`POST /api/v1/projects/{project_name}/images/push` staat correct achter `@validate_api_token`, dus de sleutel moet bij `project_name` horen. Maar de bestemming in de registry wordt gebouwd uit `image_name` en `tag` die de **aanroeper** opgeeft, zonder enige relatie tot het project:

```
docker://{REGISTRY_URL}/{REGISTRY_ORG}:{image_name}-{tag}
```

`opi/connectors/skopeo.py:117` (`_build_destination`), endpoint `opi/api/image_router.py:30`. `REGISTRY_ORG` is één platformbrede robot-account-repo (`opi/core/config.py:291`). De projectnaam komt in die bestemming niet voor.

De platte tagruimte is een **bewuste keuze**: Quay ondersteunt geen geneste repositories onder één robot-account-scope, en dat staat ook zo in het commentaar. Daar is niets mis mee. Wat ontbreekt is de eigendomscontrole die daarbij hoort.

**Wat er mis kan gaan.** Project B draait een component met tag `backend-latest`. Project A roept met zijn eigen geldige sleutel `push?image_name=backend&tag=latest` aan met een eigen tarball. Skopeo schrijft naar exact dezelfde gedeelde tag. Bij de eerstvolgende herstart of rollout van B haalt Kubernetes die image binnen (`imagePullPolicy: Always`, `project_manager.py:5345` en `manifests/deployment.yaml.jinja:76`, geen digest-pinning) en voert hem uit in de namespace van B.

Dat is code-executie bij een andere tenant, bereikbaar met niets meer dan een eigen geldige sleutel. Er is daarnaast een leesrisico: een project kan elk bekend `image:tag` als zijn eigen deployment-image opgeven en zo de image van een ander binnenhalen.

## Wat er moet gebeuren

**De registry krijgt een eigenaar per tag.** Twee wegen, en de keuze is aan de uitvoerder mits de reden erbij staat:

1. **Pinnen.** De bestemming wordt afgeleid van het project, bijvoorbeeld `{project_name}-{image_name}-{tag}`. Dan is een botsing onmogelijk in plaats van verboden, en dat is de sterkste vorm.
2. **Weigeren.** De bestaande tag wordt opgezocht en de push wordt geweigerd als hij van een ander project is. Dat patroon staat al in deze codebase: `_require_namespace_owned_by_project` doet precies dit in het restore-pad, dus er is een vorm om te volgen.

Optie 1 is te verkiezen, maar heeft een migratiekant: bestaande images dragen de oude naam. **Beslis wat er met bestaande tags gebeurt** en zeg het expliciet; stilletjes hernoemen breekt draaiende deployments, want de image-referentie van een component is vrije tekst.

**Kijk ook naar het lezen.** Een component mag nu elke image-referentie opgeven. Of dat ook begrensd moet worden is een tweede vraag; beantwoord hem in elk geval, want een gepinde push die naast een vrij lezen staat dekt maar de helft af.

## De toets

- project A kan niet meer schrijven naar een tag die project B gebruikt, en dat is met twee echte sleutels aangetoond en niet uit de code afgeleid;
- een push van A en een push van B met dezelfde `image_name` en `tag` leveren twee verschillende images op, of de tweede wordt geweigerd met een begrijpelijke melding;
- bestaande deployments blijven draaien: er staat opgeschreven wat er met bestaande tags gebeurt en dat is nagespeeld;
- de vraag over het lezen is beantwoord, ook als het antwoord "dat laten we zo" is, met de reden;
- de bevinding in `plans/technische-review-bio-en-nora-bevindingen.md` draagt de uitkomst.

## Waar op te letten

**Toon het met twee sleutels.** Dit is een tenantscheidingsfout, en de enige overtuigende toets is twee projecten die elkaar niet meer kunnen raken. Een test die één pad afdekt is hier niet genoeg; dat is bij de restore drie rondes lang misgegaan.

**Breek de bestaande weg niet.** `imagePullPolicy: Always` betekent dat een verkeerde migratie bij de eerstvolgende herstart zichtbaar wordt en niet bij de wijziging.

**Niet en passant de registry verbouwen.** De platte tagruimte is een gegeven van Quay; dit gaat over wie in die ruimte wat mag schrijven.
