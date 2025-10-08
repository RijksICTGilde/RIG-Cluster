* de SSP is de ingang om projecten te maken of beheren
* een project bevat 1 of meerdere namespaces
* een project bevat 1 of meerdere services
* bij het aanmaken van een project, worden in alle aangevraagde services de benodigde accounts etc. aangemaakt
* bij het aanmaken wordt in de vault een project aangemaakt met alle wachtwoorden en gegevens
* bij het aanmaken wordt voor alle toegang, (sealed) secrets aangemaakt die gelijk in het project
 gebruikt kunnen worden (via een GIT commit? of.. een aparte repo folder, i.v.m. sync liefst dat laatste),
 if direct gebruik via de vault API?

````
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
- github.com/organization/repo/path/to/resources?ref=v1.0.0
````

* bij het verwijderen wordt alles verwijderd

