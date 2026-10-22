# Beveiligingsbeleid

RIG-Cluster draait ZAD (Zelfservice Applicatie Deployment), een Rijksvoorziening onder [BIO2](https://www.bio-overheid.nl/).

## Een kwetsbaarheid melden

- Meld het vertrouwelijk via GitHub: [Report a vulnerability](https://github.com/RijksICTGilde/RIG-Cluster/security/advisories/new) (alleen het team ziet je melding).
- Of meld het via [Coordinated Vulnerability Disclosure van het NCSC](https://www.ncsc.nl/contact/kwetsbaarheid-melden) als je het team niet kunt bereiken.

Meld ook vermoedens en houd bevindingen vertrouwelijk totdat het team heeft kunnen reageren.

## Wat je van ons mag verwachten

- Je krijgt een reactie van het team op iedere melding.
- Kwetsbaarheden waarbij de kans op misbruik en de verwachte schade beide hoog zijn, mitigeren we zo snel mogelijk en uiterlijk binnen één week (BIO2 8.08).
- We houden je op de hoogte van de afhandeling van je melding.

## Hoe we beveiliging geregeld hebben

- **Toegang**: inloggen gaat via Keycloak SSO-Rijk ([`opi/api/auth_routes.py`](operations-manager/python/opi/api/auth_routes.py), [`opi/configs/keycloak/bootstrap.yaml`](operations-manager/python/opi/configs/keycloak/bootstrap.yaml)).
- **Autorisatie**: wat je met een project mag, hangt af van je rol in dat project ([`opi/services/project_authorization.py`](operations-manager/python/opi/services/project_authorization.py)); machine-toegang loopt over een API-sleutel per project ([`opi/utils/api_keys.py`](operations-manager/python/opi/utils/api_keys.py)).
- **Geheimen in git**: alles wat een geheim is, staat versleuteld met SOPS en AGE ([`opi/utils/sops.py`](operations-manager/python/opi/utils/sops.py), [`decrypt-sops.yaml`](infrastructure/bootstrap/infrastructure/secrets/config/overlays/odcn/decrypt-sops.yaml)).
- **Geheimen in logs**: een logfilter maskeert wachtwoorden, tokens, AGE-sleutels en API-sleutels voordat een regel wordt weggeschreven ([`opi/utils/logging_redact.py`](operations-manager/python/opi/utils/logging_redact.py)).
- **Herleidbaarheid**: wie of wat wijzigde, wordt server-side vastgesteld en gaat mee in commits en taakadministratie ([`opi/core/actor.py`](operations-manager/python/opi/core/actor.py)).
- **Secret-scan**: iedere pull request en iedere push naar `main` draait een eigen scan over de volledige boom op gecommitte geheimen ([`scripts/scan-secrets.py`](scripts/scan-secrets.py), job `secret-scan` in [`security.yml`](.github/workflows/security.yml)).
- **Dependencies**: `pip-audit` toetst de Python-dependencies tegen OSV, bij iedere pull request, bij iedere push naar `main` en wekelijks (job `dependency-audit` in [`security.yml`](.github/workflows/security.yml)).
- **Container-images**: Trivy scant de Operations Manager, de back-upimage, de SOPS-sidecar en onze eigen PostgreSQL-, Keycloak- en ArgoCD-image op kwetsbare OS-pakketten en bibliotheken; bevindingen komen als SARIF in de Security-tab van de repository (job `container-scan` in [`security.yml`](.github/workflows/security.yml)).
- **Wijzigingen**: de testsuite, de linter en de typecontrole draaien op iedere pull request en iedere push naar `main` ([`ci.yml`](.github/workflows/ci.yml)); de gevoelige paden hebben een vaste eigenaar ([`.github/CODEOWNERS`](.github/CODEOWNERS)).

De volledige BIO-verantwoording per control wordt hier niet herhaald; die is intern belegd.

## Versies

Alleen de laatst uitgerolde versie (`main` naar productie) krijgt beveiligingsfixes.
