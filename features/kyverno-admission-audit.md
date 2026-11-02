# Schot 1: admission control in waarschuwmodus

Kyverno-policies die bij admission melden wat er niet aan de afspraken voldoet, zonder iets
te blokkeren. BIO2 8.28 en de integriteitskant van de BIV-scores.

De gedachte erachter: zo waterdicht als kan, en waar we toch water maken, compartimenteren.
De scanners vinden de lekken voordat ze op het cluster staan. Dit schot kijkt naar wat er
werkelijk wordt toegelaten, ook als het langs een scanner heen kwam.

## De drie policies

`infrastructure/bootstrap/infrastructure/kyverno/policies/base/`

| Policy | Wat hij ziet |
|---|---|
| `zad-geen-latest-tag` | een image zonder tag of op `:latest`, dus zonder reproduceerbare inhoud |
| `zad-securitycontext` | een pod die als root kan draaien, kan escaleren, of een schrijfbare rootfs heeft |
| `zad-getekende-images` | een eigen image (`ghcr.io/rijksictgilde/zad/*`) zonder cosign-handtekening van de workflow die hem bouwde, keyless via de OIDC-identiteit van GitHub Actions |

Alle drie staan op `failureAction: Audit`. Ze schrijven een `PolicyReport` en laten de
workload door.

## Waarom waarschuwmodus, en wat ervoor nodig is om te blokkeren

- **De handtekening bestaat nog niet.** Het tekenen van de eigen images hangt aan het plan
  over de getekende en geattesteerde build. Tot die er is meldt `zad-getekende-images` alles
  wat we zelf bouwen, en dat is precies de nulmeting die je wilt zien voordat je afdwingt.
- **Read-only rootfs is per workload een beslissing.** Een tenant-image is niet van ons; zie
  dezelfde afweging in `features/aanvalsproef-harnas.md`.
- **Blokkeren raakt de uitrol.** Welke build productie draait en wat er gebeurt als een
  policy een deployment tegenhoudt is een eigen besluit, niet een bijwerking van dit plan.

De weg naar enforce is per policy dezelfde: zet hem op `Enforce` zodra het rapport leeg is
en er een afspraak ligt over wie een uitzondering mag geven.

## Hoe je het opzet

```bash
task schotten:kyverno     # installeert Kyverno en zet de policies erop
kubectl get clusterpolicy
kubectl get policyreport -A
```

De Kyverno-versie staat op een plek: de env-pin `KYVERNO_VERSION` in
`.github/workflows/aanvalsproef.yml`. De Taskfile-taak leest hem daaruit met `yq`, zodat een
handmatige uitrol dezelfde versie draait als de CI-toets.

De policies staan nog niet in de GitOps-boom (`infrastructure/bootstrap/infrastructure/kustomization.yaml`
noemt ze niet). Dat is bewust: de CRD's van Kyverno moeten er zijn voordat een ClusterPolicy
kan syncen, en een ArgoCD-app die daarop struikelt raakt de hele infrastructuurboom. Dat
koppelen hoort bij de stap naar enforce.

## Wat de toets bewijst

De job `admission-audit` in `.github/workflows/aanvalsproef.yml` doet het in twee stappen.

Eerst zonder cluster, met de Kyverno CLI, tegen drie manifests in `.github/aanvalsproef/`:

```bash
kyverno apply infrastructure/bootstrap/infrastructure/kyverno/policies/base/geen-latest-tag.yaml \
              infrastructure/bootstrap/infrastructure/kyverno/policies/base/securitycontext.yaml \
  --resource .github/aanvalsproef/overtreder-pod.yaml \
  --resource .github/aanvalsproef/tagloze-pod.yaml \
  --resource .github/aanvalsproef/nette-pod.yaml
# pass: 5, fail: 4
```

De overtreder (`:latest`, geen securityContext) faalt op alle drie de regels, de tagloze pod
alleen op de tag-regel, en de nette pod haalt ze alle drie. De job eist die exacte uitkomst:
een policy die niets meer meet, valt op.

Daarna op een Kind-cluster: Kyverno erop, de policies erop, de overtreder aanmelden. Die
wordt gescheduled (dus niet geblokkeerd) en komt met beide overtredingen in het
`PolicyReport`. Dat is de meting die zegt dat waarschuwmodus doet wat de naam belooft.

`zad-getekende-images` zit niet in de CLI-stap: `verifyImages` vraagt de registry en een
echte handtekening, en de CLI slaat de regel dan over.

Gemeten met de gepinde CLI 1.19.2: dezelfde `pass: 5, fail: 4`. Die versie waarschuwt wel dat
`kyverno.io/v1 ClusterPolicy` verdwijnt in een latere release, met `ValidatingPolicy`
(`policies.kyverno.io`) als opvolger. De waarschuwing laat de uitkomst heel; de migratie hoort
bij de stap naar enforce, net als het koppelen aan de GitOps-boom.
