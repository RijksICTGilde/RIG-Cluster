# De backup- en restorepods krijgen het serviceaccount dat er al is

Status: plan, 17 september 2026. Niet gebouwd. Ter kennisgeving afgesplitst uit de review van RC-177 (PR #165), dat inmiddels gemerged is.

## Wat er is, gemeten

RC-177 gaf elk project een eigen serviceaccount (`<project>-sa`) zonder pull-secrets. De reden: op ODCN hangt de GlobalTenantResource `pullsecrets-rig` zijn secrets aan élk `default` serviceaccount, en een pod die op `default` draait erft die hele stapel. Met een eigen serviceaccount draagt een pod alleen het pull-secret dat hij zelf nodig heeft.

Drie sjablonen gebruiken dat account al:

```
manifests/deployment.yaml.jinja:54    serviceAccountName: {{ service_account_name }}
manifests/job-pod.yaml.jinja:29       serviceAccountName: {{ service_account_name }}
manifests/db-console-pod.yaml.jinja   (aanwezig)
```

Drie niet, en die draaien dus op `default`:

```
manifests/backup-pod.yaml.jinja
manifests/restore-pod.yaml.jinja
manifests/backup-bucket-mirror-pod.yaml.jinja
```

Dat is precies de erfenis die RC-177 wilde wegnemen, op de pods die de back-ups van het project aanraken.

## Wat er moet gebeuren

Zet `serviceAccountName` op de drie sjablonen, in dezelfde vorm als `job-pod.yaml.jinja` die al gebruikt. Geen nieuw account, geen nieuwe rechten: het account bestaat en de andere pods gebruiken het.

Ga per sjabloon na dat de aanroeper de variabele ook meegeeft. Een Jinja-variabele die niet gevuld wordt, levert een leeg veld op en dan valt de pod stil terug op `default`, wat er van buiten uitziet alsof de wijziging is doorgevoerd. Dat is de enige echte val in deze taak.

## De toets

- de drie sjablonen dragen `serviceAccountName`, en het gerenderde manifest bevat een niet-lege waarde. Toets het gerenderde resultaat, niet de aanwezigheid van de regel in het sjabloon;
- `grep -L serviceAccountName manifests/*pod*.jinja` levert niets meer op;
- een back-up en een restore lopen nog. Dit is een wijziging aan een pod die met opslag praat, dus dat moet echt gedraaid zijn en niet alleen gerenderd;
- op een cluster zonder de GlobalTenantResource verandert er niets zichtbaars: het account bestaat daar ook en draagt daar simpelweg geen erfenis.

## Waar op te letten

**Dit verandert wie de pod is, dus kijk naar de rechten die hij nodig had.** Draaide de backup-pod ergens op een recht dat aan `default` hing in plaats van aan `<project>-sa`, dan breekt hij hier. Dat is niet te zien aan het manifest; dat is te zien aan een back-up die daadwerkelijk draait. Vandaar dat die stap in de toets staat.

**Dit is de laatste van de RC-177-reeks die nog op de erfenis leunt.** Als deze binnen is, draait geen enkele projectpod meer op `default`.
