"""DNS-1123-vorm op ``subdomain`` en ``base_domain`` (RC-327 stap 7).

Het model toetst alleen de VORM; of een domein ondersteund of goedgekeurd is, blijft bij
``validate_base_domain`` en de goedkeuringsroute. Deze tests dekken alleen de vorm.
"""

from __future__ import annotations

import pytest
from opi.connectors.subdomain import validate_subdomain_for_domain
from opi.core import config as opi_config
from opi.core.project_schema import validate_project_schema
from opi.manager.project_validation import ProjectIntegrityError, validate_project_structure
from opi.services.catalog.publish_on_web.config_model import PublishOnWebDeploymentConfig
from opi.services.schema_migration import LATEST_SCHEMA_VERSION_VALUE, migrate_to_latest
from pydantic import ValidationError


@pytest.mark.parametrize("value", ["rijksapp.nl", "domein.nl", "sub.domein.nl"])
def test_geldige_base_domains_passeren(value: str) -> None:
    model = PublishOnWebDeploymentConfig(**{"base-domain": value})
    assert model.base_domain == value


@pytest.mark.parametrize("value", ["mijn-app"])
def test_geldige_subdomains_passeren(value: str) -> None:
    model = PublishOnWebDeploymentConfig(subdomain=value)
    assert model.subdomain == value


def test_geen_waarde_passeert() -> None:
    model = PublishOnWebDeploymentConfig()
    assert model.base_domain is None
    assert model.subdomain is None


@pytest.mark.parametrize(
    "value",
    [
        "Rijksapp.nl",  # hoofdletter, een gestored waarde moet al lowercase zijn
        "mijn_app.nl",  # underscore
        "mijn app.nl",  # spatie
        "domein.nl\n",  # regeleinde
        "a" * 64 + ".nl",  # een label van 64 tekens
        "eenlabel",  # geen punt, dat is geen domein
    ],
)
def test_ongeldige_base_domains_worden_geweigerd(value: str) -> None:
    with pytest.raises(ValidationError):
        PublishOnWebDeploymentConfig(**{"base-domain": value})


@pytest.mark.parametrize(
    "value",
    [
        "Mijn-App",  # hoofdletter
        "mijn_app",  # underscore
        "mijn app",  # spatie
        "mijn-app\n",  # regeleinde
        "a" * 64,  # 64 tekens
        "-mijn-app",  # begint met koppelteken
    ],
)
def test_ongeldige_subdomains_worden_geweigerd(value: str) -> None:
    with pytest.raises(ValidationError):
        PublishOnWebDeploymentConfig(subdomain=value)


def _project_met_subdomein(subdomain: str, *, cluster: str, base_domain: str) -> dict:
    """De vorm van ``amtbz-2m9``: publish-on-web op de deployment, met een basisdomein."""
    return {
        "schema-version": LATEST_SCHEMA_VERSION_VALUE,
        "name": "amtbz-2m9",
        "description": "Project created via self-service portal",
        "clusters": [cluster],
        "users": [{"email": "admin@rijksoverheid.nl", "role": "admin"}],
        "services": ["publish-on-web"],
        "repositories": [
            {"name": "main-repo", "url": "https://github.com/rig/amtbz.git", "branch": "main", "path": "."}
        ],
        "components": [
            {
                "name": "frontend",
                "type": "frontend",
                "ports": {"inbound": [8080], "outbound": [443]},
                "path": [{"match": "/"}],
                "services": ["publish-on-web"],
            }
        ],
        "deployments": [
            {
                "name": "productie",
                "cluster": cluster,
                "namespace": "amtbz-2m9",
                "repository": "main-repo",
                "components": [{"reference": "frontend", "image": "nginx:latest"}],
                "services": [
                    {
                        "reference": "publish-on-web",
                        "config": {
                            "base-domain": base_domain,
                            "subdomain": subdomain,
                            "domain-format": "subdomain",
                        },
                    }
                ],
            }
        ],
    }


class TestEenSubdomeinMetPunten:
    """RC-362: de vorm laat punten door, het domein beslist of ze mogen.

    ``amtbz-2m9`` draait in productie met ``subdomain: amt.bzk`` op ``rijksapp.nl``. De
    vormtoets van RC-327 eiste één DNS-label en maakte dat bestand onopslaanbaar.
    """

    def test_de_vorm_laat_punten_door(self) -> None:
        assert PublishOnWebDeploymentConfig(subdomain="amt.bzk").subdomain == "amt.bzk"

    @pytest.mark.parametrize(
        "value",
        [
            "Amt.bzk",  # hoofdletter
            "amt_bzk.nl",  # underscore
            "amt bzk",  # spatie
            "amt.bzk\n",  # regeleinde
            "a..b",  # leeg deel
            ".amt",  # leeg eerste deel
            "amt.",  # leeg laatste deel
            "a" * 64 + ".bzk",  # een deel van 64 tekens
        ],
    )
    def test_een_ongeldige_vorm_blijft_geweigerd(self, value: str) -> None:
        with pytest.raises(ValidationError):
            PublishOnWebDeploymentConfig(subdomain=value)

    async def test_punten_mogen_op_een_domein_dat_ze_toestaat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(opi_config.settings, "CLUSTER_MANAGER", "odcn-production")
        project = _project_met_subdomein("amt.bzk", cluster="odcn-production", base_domain="rijksapp.nl")

        migrated, _ = migrate_to_latest(project)
        validate_project_schema(migrated)
        await validate_project_structure(migrated)

    async def test_punten_mogen_niet_op_een_domein_dat_ze_niet_toestaat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(opi_config.settings, "CLUSTER_MANAGER", "sandboxed-local")
        project = _project_met_subdomein("amt.bzk", cluster="sandboxed-local", base_domain="sandbox.rijksapp.dev")

        migrated, _ = migrate_to_latest(project)
        validate_project_schema(migrated)
        with pytest.raises(ProjectIntegrityError, match="ondersteunt geen punten in het subdomein"):
            await validate_project_structure(migrated)

    @pytest.mark.parametrize(
        ("cluster", "base_domain", "geldig"),
        [("odcn-production", "rijksapp.nl", True), ("sandboxed-local", "sandbox.rijksapp.dev", False)],
    )
    def test_de_publicatieweg_volgt_dezelfde_regel(self, cluster: str, base_domain: str, geldig: bool) -> None:
        # De registratie (subdomain_registry) vraagt zonder override, dus die moet de regel
        # uit de clusterconfig halen; anders strandt amtbz-2m9 alsnog bij het verwerken.
        is_valid, _ = validate_subdomain_for_domain("amt.bzk", base_domain, cluster)
        assert is_valid is geldig
