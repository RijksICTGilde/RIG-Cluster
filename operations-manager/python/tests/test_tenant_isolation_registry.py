"""Tenant isolation on the shared container registry (RC-98).

The image-push endpoint writes into ONE registry repository, because Quay has no
nested repos under a single robot-account scope. Ownership therefore lives in the
tag, and these tests prove it holds from the outside: two projects, two real API
keys, the same ``image_name`` and ``tag``, and two different destinations.

They deliberately drive the real ``SkopeoConnector`` (only the subprocess and the
availability check are mocked) rather than a stubbed one, so the destination that is
asserted is the destination skopeo would be handed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.image_router import image_router
from opi.connectors.skopeo import SkopeoConnector
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.project_validation import validate_platform_registry_image_ownership, validate_project_structure
from opi.services.catalog.image_registries.naming import organization_name

PROJECT_A = "project-a"
PROJECT_B = "project-b"
KEY_A = "key-of-project-a"
KEY_B = "key-of-project-b"

REGISTRY_URL = "rcr.rijksapps.nl"
REGISTRY_ORG = "rig/zad"
PLATFORM_REPO = f"{REGISTRY_URL}/{REGISTRY_ORG}"


@pytest.fixture(autouse=True)
def _reset_skopeo_singleton():
    SkopeoConnector._instance = None
    yield
    SkopeoConnector._instance = None


def _project(name: str, api_key: str) -> MagicMock:
    project = MagicMock()
    project.name = name
    project.api_key = api_key
    return project


@pytest.fixture
def two_projects():
    """A project store holding two projects, each with its own API key."""
    projects = {PROJECT_A: _project(PROJECT_A, KEY_A), PROJECT_B: _project(PROJECT_B, KEY_B)}
    store = MagicMock()
    store.get.side_effect = projects.get
    with patch("opi.api.endpoint_util.get_project_store", return_value=store):
        yield store


@pytest.fixture
def push_destinations():
    """Run the real connector against a mocked skopeo, recording every destination."""
    destinations: list[str] = []

    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b"", b"")

    def record(*cmd: str, **_kwargs: object) -> AsyncMock:
        destinations.append(cmd[-1])
        return process

    with (
        patch("opi.connectors.skopeo.subprocess.run") as version_check,
        patch("opi.connectors.skopeo.decrypt_password_smart_auto_sync", return_value="token"),
        patch("asyncio.create_subprocess_exec", side_effect=record),
        patch("opi.connectors.skopeo.settings") as connector_settings,
        patch("opi.api.image_router.settings") as router_settings,
    ):
        version_check.return_value = MagicMock(returncode=0, stdout="skopeo version 1.14.0", stderr="")
        for mocked in (connector_settings, router_settings):
            mocked.REGISTRY_URL = REGISTRY_URL
            mocked.REGISTRY_ORG = REGISTRY_ORG
        connector_settings.REGISTRY_USERNAME = "rig+zad"
        connector_settings.REGISTRY_PASSWORD = "age:token"
        connector_settings.REGISTRY_VERIFY_TLS = True
        router_settings.IMAGE_UPLOAD_MAX_SIZE_MB = 10
        router_settings.TEMP_DIR = "/tmp"
        yield destinations


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(image_router)
    return TestClient(app)


def _push(client: TestClient, project: str, api_key: str, image_name: str = "backend", tag: str = "latest"):
    return client.post(
        f"/api/v1/projects/{project}/images/push?image_name={image_name}&tag={tag}",
        headers={"X-API-Key": api_key},
        files={"file": (f"{image_name}.tar", b"fake-tarball")},
    )


class TestPushOwnership:
    """The write half: project A cannot land on the tag project B pushed to."""

    def test_two_keys_same_image_and_tag_land_on_different_tags(self, client, two_projects, push_destinations):
        response_a = _push(client, PROJECT_A, KEY_A)
        response_b = _push(client, PROJECT_B, KEY_B)

        assert response_a.status_code == 200
        assert response_b.status_code == 200

        assert push_destinations == [
            f"docker://{PLATFORM_REPO}:{PROJECT_A}_backend-latest",
            f"docker://{PLATFORM_REPO}:{PROJECT_B}_backend-latest",
        ]
        assert response_a.json()["image"] == f"{PLATFORM_REPO}:{PROJECT_A}_backend-latest"
        assert response_b.json()["image"] == f"{PLATFORM_REPO}:{PROJECT_B}_backend-latest"
        assert response_a.json()["image"] != response_b.json()["image"]

    def test_project_a_cannot_reach_the_tag_of_project_b(self, client, two_projects, push_destinations):
        """Whatever A supplies, the destination keeps A's owner prefix.

        This is the attack from the review: B runs 'backend:latest', A pushes the same
        names with its own key. A also tries to smuggle B's name in through image_name.
        """
        _push(client, PROJECT_B, KEY_B)
        b_destination = push_destinations[-1]

        for image_name in ("backend", f"{PROJECT_B}-backend", f"{PROJECT_B}_backend"):
            _push(client, PROJECT_A, KEY_A, image_name=image_name)
            assert push_destinations[-1] != b_destination
            assert push_destinations[-1].startswith(f"docker://{PLATFORM_REPO}:{PROJECT_A}_")

    def test_a_key_of_another_project_is_still_rejected(self, client, two_projects, push_destinations):
        response = _push(client, PROJECT_B, KEY_A)
        assert response.status_code == 401
        assert push_destinations == []

    def test_the_authenticated_project_owns_the_tag_not_the_path(self, client, push_destinations):
        """The owner comes from the key's project, not from the path segment.

        ``validate_api_token`` overwrites ``project_name`` with the store's project
        name, so a path that differs in case or shape cannot change the owner.
        """
        store = MagicMock()
        store.get.side_effect = {"PROJECT-A": _project(PROJECT_A, KEY_A)}.get
        with patch("opi.api.endpoint_util.get_project_store", return_value=store):
            response = _push(client, "PROJECT-A", KEY_A)

        assert response.status_code == 200
        assert push_destinations == [f"docker://{PLATFORM_REPO}:{PROJECT_A}_backend-latest"]


class TestReadOwnership:
    """The read half: a deployment may not point at another project's tag."""

    @pytest.fixture(autouse=True)
    def _registry_configured(self):
        with patch("opi.manager.project_validation.settings") as mocked:
            mocked.REGISTRY_URL = REGISTRY_URL
            mocked.REGISTRY_ORG = REGISTRY_ORG
            yield

    @staticmethod
    def _project_with_image(image: str) -> dict:
        return {
            "name": PROJECT_A,
            "deployments": [{"name": "production", "components": [{"reference": "web", "image": image}]}],
        }

    def test_own_image_is_accepted(self):
        data = self._project_with_image(f"{PLATFORM_REPO}:{PROJECT_A}_backend-latest")
        assert validate_platform_registry_image_ownership(data) == []

    def test_image_of_another_project_is_rejected(self):
        data = self._project_with_image(f"{PLATFORM_REPO}:{PROJECT_B}_backend-latest")
        errors = validate_platform_registry_image_ownership(data)
        assert len(errors) == 1
        assert PROJECT_B in errors[0]
        assert "zelf gepusht" in errors[0]

    def test_legacy_unowned_tag_stays_usable(self):
        """Tags pushed before pinning have no owner and must keep working."""
        data = self._project_with_image(f"{PLATFORM_REPO}:backend-latest")
        assert validate_platform_registry_image_ownership(data) == []

    def test_an_uppercase_host_is_still_the_platform_registry(self):
        """A hostname is case-insensitive, so shouting it must not dodge the check."""
        image = f"{REGISTRY_URL.upper()}/{REGISTRY_ORG}:{PROJECT_B}_backend-latest"
        errors = validate_platform_registry_image_ownership(self._project_with_image(image))
        assert len(errors) == 1
        assert PROJECT_B in errors[0]

    def test_an_explicit_https_port_is_still_the_platform_registry(self):
        """':443' is the port the reference already implies, not another registry."""
        image = f"{REGISTRY_URL}:443/{REGISTRY_ORG}:{PROJECT_B}_backend-latest"
        errors = validate_platform_registry_image_ownership(self._project_with_image(image))
        assert len(errors) == 1
        assert PROJECT_B in errors[0]

    def test_a_digest_reference_into_the_platform_registry_is_refused(self):
        """A digest names an image in the shared repo without naming its owner."""
        digest = "sha256:" + "ab" * 32
        for image in (f"{PLATFORM_REPO}@{digest}", f"{PLATFORM_REPO}:{PROJECT_A}_backend-latest@{digest}"):
            errors = validate_platform_registry_image_ownership(self._project_with_image(image))
            assert len(errors) == 1, image
            assert "digest" in errors[0]

    def test_a_digest_outside_the_platform_registry_stays_free(self):
        digest = "sha256:" + "ab" * 32
        image = f"ghcr.io/rijksictgilde/algoritmeregister/backend@{digest}"
        assert validate_platform_registry_image_ownership(self._project_with_image(image)) == []

    def test_images_outside_the_platform_registry_are_not_judged(self):
        for image in (
            "ghcr.io/rijksictgilde/algoritmeregister/backend:project-b_thing-v1",
            "nginx:latest",
            f"{REGISTRY_URL}/other-org:{PROJECT_B}_backend-latest",
        ):
            assert validate_platform_registry_image_ownership(self._project_with_image(image)) == []

    def test_missing_registry_configuration_disables_the_check(self):
        with patch("opi.manager.project_validation.settings") as mocked:
            mocked.REGISTRY_URL = ""
            mocked.REGISTRY_ORG = ""
            data = self._project_with_image(f"{PLATFORM_REPO}:{PROJECT_B}_backend-latest")
            assert validate_platform_registry_image_ownership(data) == []

    @pytest.mark.asyncio
    async def test_structure_validation_refuses_to_save_a_foreign_image(self):
        data = {
            "name": PROJECT_A,
            "components": [{"name": "web", "path": "/"}],
            "deployments": [
                {
                    "name": "production",
                    "components": [{"reference": "web", "image": f"{PLATFORM_REPO}:{PROJECT_B}_backend-latest"}],
                }
            ],
        }
        with pytest.raises(ProjectIntegrityError, match=PROJECT_B):
            await validate_project_structure(data)


# ---------------------------------------------------------------------------
# De proxy-organisaties: dezelfde vraag, de andere registry (RC-177)
# ---------------------------------------------------------------------------


class TestProxyOrganizationOwnership:
    """Elke pod in de tenant krijgt het gerepliceerde pull-secret van elk project, dus wie
    de naam van andermans proxy-organisatie kent kan er met ANDERMANS credentials uit
    lezen. Dat is dezelfde weigering als bij de gedeelde platformregistry, een registry
    verder."""

    @staticmethod
    def _project(image: str, name: str = "eigen") -> dict:
        return {
            "name": name,
            "deployments": [
                {
                    "name": "prod",
                    "cluster": "odcn-production",
                    "namespace": name,
                    "components": [{"reference": "web", "image": image}],
                }
            ],
        }

    @staticmethod
    def _store(*project_names: str):
        from unittest.mock import MagicMock, patch

        store = MagicMock()
        store.get_all.return_value = [MagicMock(name=n) for n in project_names]
        # MagicMock(name=...) zet de REPR en niet het attribuut; zet hem expliciet.
        for summary, project_name in zip(store.get_all.return_value, project_names, strict=True):
            summary.name = project_name
        return patch("opi.services.project_store.get_project_store", return_value=store)

    def test_andermans_organisatie_wordt_geweigerd(self) -> None:
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_ownership

        organisatie = organization_name("code.overheid.nl/x", "rig", "ander")
        data = self._project(f"rcr.rijksapps.nl/{organisatie}/app:1")
        with self._store("eigen", "ander"):
            errors = validate_proxy_organization_ownership(data)
        assert len(errors) == 1
        assert "ander" in errors[0]

    def test_andermans_organisatie_met_upstream_namespace_wordt_ook_geweigerd(self) -> None:
        """De suffix draagt achter de projectnaam nog de upstream-hash, dus de projectnaam
        staat niet aan het EIND van de organisatienaam."""
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_ownership

        data = self._project(f"rcr.rijksapps.nl/{organization_name('ghcr.io/teamx', 'rig', 'ander')}/app:1")
        with self._store("eigen", "ander"):
            errors = validate_proxy_organization_ownership(data)
        assert len(errors) == 1
        assert "ander" in errors[0]

    def test_een_project_dat_naar_een_hash_heet_eist_niets_van_een_ander_op(self) -> None:
        """De spiegelkant van de eerste blokkerende vondst: nu de organisatie op een hash
        EINDIGT zou een kale ``endswith`` op de projectnaam de organisatie van ``eigen``
        aan een project ``eigen-<hash>`` toewijzen, en dan wordt de eigenaar zelf
        geweigerd op zijn eigen image."""
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_ownership

        organisatie = organization_name("ghcr.io/x", "rig", "eigen")
        naamgenoot = organisatie.rsplit("-", 1)[1]
        data = self._project(f"rcr.rijksapps.nl/{organisatie}/app:1")
        with self._store("eigen", f"eigen-{naamgenoot}"):
            assert validate_proxy_organization_ownership(data) == []

    def test_een_langere_projectnaam_wordt_niet_voor_een_kortere_aangezien(self) -> None:
        """Op segmentgrens: ``demo`` mag de organisaties van ``demonstratie`` niet opeisen."""
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_ownership

        organisatie = organization_name("ghcr.io/x", "rig", "demonstratie")
        data = self._project(f"rcr.rijksapps.nl/{organisatie}/app:1", name="demonstratie")
        with self._store("demonstratie", "demo"):
            assert validate_proxy_organization_ownership(data) == []

    def test_de_eigen_organisatie_mag(self) -> None:
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_ownership

        data = self._project(f"rcr.rijksapps.nl/{organization_name('code.overheid.nl/x', 'rig', 'eigen')}/app:1")
        with self._store("eigen", "ander"):
            assert validate_proxy_organization_ownership(data) == []

    def test_een_gedeelde_proxy_mag(self) -> None:
        """ghcr-rig en code-overheid-rig eindigen niet op een projectnaam; die zijn van
        iedereen."""
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_ownership

        for organization in ("ghcr-rig", "code-overheid-rig", "dockerhub-rig"):
            data = self._project(f"rcr.rijksapps.nl/{organization}/x/app:1")
            with self._store("eigen", "ander"):
                assert validate_proxy_organization_ownership(data) == [], organization

    def test_een_image_buiten_de_proxy_registry_gaat_dit_niet_aan(self) -> None:
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_ownership

        data = self._project(f"ghcr.io/{organization_name('ghcr.io/x', 'rig', 'ander')}/app:1")
        with self._store("eigen", "ander"):
            assert validate_proxy_organization_ownership(data) == []

    def test_een_hoofdletterhost_en_poort_ontsnappen_niet(self) -> None:
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_ownership

        organisatie = organization_name("code.overheid.nl/x", "rig", "ander")
        for image in (
            f"RCR.rijksapps.nl/{organisatie}/app:1",
            f"rcr.rijksapps.nl:443/{organisatie}/app:1",
        ):
            with self._store("eigen", "ander"):
                assert validate_proxy_organization_ownership(self._project(image)), image

    def test_een_cluster_zonder_proxy_operator_zegt_niets(self) -> None:
        """Op sandbox bestaat er geen proxy-organisatie, dus valt er ook niets te weigeren."""
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_ownership

        data = self._project(f"rcr.rijksapps.nl/{organization_name('ghcr.io/x', 'rig', 'ander')}/app:1")
        data["deployments"][0]["cluster"] = "sandboxed-local"
        with self._store("eigen", "ander"):
            assert validate_proxy_organization_ownership(data) == []

    def test_zonder_andere_projecten_valt_er_niets_te_weigeren(self) -> None:
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_ownership

        data = self._project(f"rcr.rijksapps.nl/{organization_name('ghcr.io/x', 'rig', 'ander')}/app:1")
        with self._store("eigen"):
            assert validate_proxy_organization_ownership(data) == []


class TestRegistryEntryOwnership:
    """De tweede blokkerende vondst uit de securityreview.

    ``validate_proxy_organization_ownership`` loopt over de IMAGES in het projectbestand,
    maar een registry-ENTRY is zelf al genoeg: ``registry_rule()`` maakt er een regel van
    die op elke image onder die upstream slaat en het opgegeven ``secretName`` eraan hangt.
    Sinds ``apply_bundle`` de projectregels meekrijgt geldt dat ook voor een ad-hoc jobpod
    met een door de gebruiker ingetypte image, en die komt langs geen enkele validator.
    """

    @staticmethod
    def _project(registry: dict, name: str = "aanvaller") -> dict:
        return {
            "name": name,
            "clusters": ["odcn-production"],
            "services": [{"name": "image-registries", "config": {"registries": [registry]}}],
        }

    @staticmethod
    def _store(*project_names: str):
        from unittest.mock import MagicMock, patch

        summaries = []
        for project_name in project_names:
            summary = MagicMock()
            summary.name = project_name
            summary.data = None
            summaries.append(summary)
        store = MagicMock()
        store.get_all.return_value = summaries
        return patch("opi.services.project_store.get_project_store", return_value=store)

    def test_een_upstream_naar_andermans_proxy_organisatie_wordt_geweigerd(self) -> None:
        from opi.services.catalog.image_registries.ownership import validate_registry_entry_ownership

        organisatie = organization_name("ghcr.io/team", "rig", "slachtoffer")
        data = self._project({"name": "buit", "upstream": f"rcr.rijksapps.nl/{organisatie}"})
        with self._store("aanvaller", "slachtoffer"):
            errors = validate_registry_entry_ownership(data)
        assert len(errors) == 1
        assert "slachtoffer" in errors[0]
        assert "upstream" in errors[0]

    def test_andermans_robot_pull_secret_wordt_geweigerd(self) -> None:
        """De andere helft van dezelfde regel: de upstream bepaalt WELKE images hij raakt,
        het secretName bepaalt WELK credential eraan hangt."""
        from opi.services.catalog.image_registries.ownership import validate_registry_entry_ownership

        organisatie = organization_name("ghcr.io/team", "rig", "slachtoffer")
        data = self._project(
            {"name": "buit", "upstream": "ghcr.io/eigen", "secretName": f"{organisatie}-robot-pull-secret"}
        )
        with self._store("aanvaller", "slachtoffer"):
            errors = validate_registry_entry_ownership(data)
        assert len(errors) == 1
        assert "slachtoffer" in errors[0]
        assert "secretName" in errors[0]

    def test_de_eigen_organisatie_mag(self) -> None:
        from opi.services.catalog.image_registries.ownership import validate_registry_entry_ownership

        organisatie = organization_name("ghcr.io/team", "rig", "aanvaller")
        data = self._project(
            {
                "name": "eigen",
                "upstream": f"rcr.rijksapps.nl/{organisatie}",
                "secretName": f"{organisatie}-robot-pull-secret",
            }
        )
        with self._store("aanvaller", "slachtoffer"):
            assert validate_registry_entry_ownership(data) == []

    def test_een_gedeelde_proxy_mag(self) -> None:
        """Het dp-bn7-geval: ``rcr.rijksapps.nl/rig`` met het platform-robotsecret."""
        from opi.services.catalog.image_registries.ownership import validate_registry_entry_ownership

        data = self._project(
            {"name": "platform", "upstream": "rcr.rijksapps.nl/rig", "secretName": "rig-robot-pull-secret"}
        )
        with self._store("aanvaller", "slachtoffer"):
            assert validate_registry_entry_ownership(data) == []

    def test_een_gewone_private_registry_mag(self) -> None:
        from opi.services.catalog.image_registries.ownership import validate_registry_entry_ownership

        data = self._project({"name": "eigen", "upstream": "code.overheid.nl/robbert.uittenbroek"})
        with self._store("aanvaller", "slachtoffer"):
            assert validate_registry_entry_ownership(data) == []

    def test_op_een_cluster_zonder_proxy_operator_valt_er_niets_te_weigeren(self) -> None:
        from opi.services.catalog.image_registries.ownership import validate_registry_entry_ownership

        organisatie = organization_name("ghcr.io/team", "rig", "slachtoffer")
        data = self._project({"name": "buit", "upstream": f"rcr.rijksapps.nl/{organisatie}"})
        data["clusters"] = ["sandboxed-local"]
        with self._store("aanvaller", "slachtoffer"):
            assert validate_registry_entry_ownership(data) == []


class TestProxyOrganizationClaims:
    """De organisatienaam is TENANTBREED: twee CR's met dezelfde ``metadata.name`` in twee
    namespaces sturen EEN organisatie in RCR aan, en het gelijknamige robot-pull-secret
    wordt naar allebei de namespaces gerepliceerd. De naamregel maakt dat bij normaal
    gebruik onmogelijk; deze grendel meet de UITKOMST, zodat afkapping op 63 tekens of een
    latere naamwijziging bij het OPSLAAN sneuvelt en niet pas bij het reconcileren.
    """

    #: Lang genoeg dat ``<friendly>-rig-<projectnaam>-<hash>`` over de 63 tekens heen gaat.
    LANGE_UPSTREAM = "registry." + "a" * 30 + ".example.nl/team"

    @staticmethod
    def _project(registries: list[dict], name: str) -> dict:
        return {
            "name": name,
            "clusters": ["odcn-production"],
            "services": [{"name": "image-registries", "config": {"registries": registries}}],
        }

    def _store(self, *projects: dict):
        from unittest.mock import MagicMock, patch

        summaries = []
        for project in projects:
            summary = MagicMock()
            summary.name = project["name"]
            summary.data = project
            summaries.append(summary)
        store = MagicMock()
        store.get_all.return_value = summaries
        return patch("opi.services.project_store.get_project_store", return_value=store)

    def test_twee_projecten_op_een_organisatienaam_worden_geweigerd(self) -> None:
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_claims

        ander = self._project([{"name": "r", "upstream": self.LANGE_UPSTREAM}], "aaaaaaaaaaaaaaaaaaab")
        eigen = self._project([{"name": "r", "upstream": self.LANGE_UPSTREAM}], "aaaaaaaaaaaaaaaaaaac")
        # Tegenproef: het zijn werkelijk twee verschillende projecten.
        assert organization_name(self.LANGE_UPSTREAM, "rig", eigen["name"]) == organization_name(
            self.LANGE_UPSTREAM, "rig", ander["name"]
        )
        with self._store(eigen, ander):
            errors = validate_proxy_organization_claims(eigen)
        assert len(errors) == 1
        assert ander["name"] in errors[0]

    def test_twee_gewone_projecten_botsen_niet(self) -> None:
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_claims

        ander = self._project([{"name": "r", "upstream": "ghcr.io/team"}], "ander")
        eigen = self._project([{"name": "r", "upstream": "ghcr.io/team"}], "eigen")
        with self._store(eigen, ander):
            assert validate_proxy_organization_claims(eigen) == []

    def test_twee_entries_van_hetzelfde_project_op_een_organisatie_worden_geweigerd(self) -> None:
        """Een bestandsnaam op het projectniveau, dus de tweede Organization overschrijft
        stil de eerste terwijl ``build_rules`` wel twee regels naar die ene bestemming
        levert."""
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_claims

        eigen = self._project(
            [
                {"name": "een", "upstream": "ghcr.io/team"},
                {"name": "twee", "upstream": "ghcr.io/team"},
            ],
            "eigen",
        )
        with self._store(eigen):
            errors = validate_proxy_organization_claims(eigen)
        assert len(errors) == 1
        assert "twee" in errors[0]
        assert "een" in errors[0]

    def test_twee_verschillende_upstreams_van_een_project_mogen(self) -> None:
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_claims

        eigen = self._project(
            [
                {"name": "een", "upstream": "ghcr.io/orga"},
                {"name": "twee", "upstream": "ghcr.io/orgb"},
            ],
            "eigen",
        )
        with self._store(eigen):
            assert validate_proxy_organization_claims(eigen) == []

    def test_op_een_cluster_zonder_proxy_operator_valt_er_niets_te_claimen(self) -> None:
        from opi.services.catalog.image_registries.ownership import validate_proxy_organization_claims

        ander = self._project([{"name": "r", "upstream": self.LANGE_UPSTREAM}], "aaaaaaaaaaaaaaaaaaab")
        eigen = self._project([{"name": "r", "upstream": self.LANGE_UPSTREAM}], "aaaaaaaaaaaaaaaaaaac")
        eigen["clusters"] = ["sandboxed-local"]
        with self._store(eigen, ander):
            assert validate_proxy_organization_claims(eigen) == []


class TestDeGrendelsZittenAanDeSavePoort:
    """Niet of de functie werkt, maar of de save-poort hem AANROEPT.

    ``validate_project_structure`` is de enige poort waar elke schrijfroute langs komt
    (``ProjectStore._validate``, en dus ook de API-endpoints die een registry-entry
    wegschrijven). Een toets die er niet aan hangt is geen grendel.
    """

    @staticmethod
    def _store(*projects: dict):
        from unittest.mock import MagicMock, patch

        summaries = []
        for project in projects:
            summary = MagicMock()
            summary.name = project["name"]
            summary.data = project
            summaries.append(summary)
        store = MagicMock()
        store.get_all.return_value = summaries
        return patch("opi.services.project_store.get_project_store", return_value=store)

    @staticmethod
    def _with_registry(name: str, registry: dict) -> dict:
        return {
            "name": name,
            "clusters": ["odcn-production"],
            "components": [{"name": "web", "path": "/"}],
            "services": [{"name": "image-registries", "config": {"registries": [registry]}}],
        }

    @pytest.mark.asyncio
    async def test_een_entry_naar_andermans_organisatie_wordt_niet_opgeslagen(self):
        organisatie = organization_name("ghcr.io/team", "rig", "slachtoffer")
        eigen = self._with_registry(
            "aanvaller",
            {
                "name": "buit",
                "upstream": f"rcr.rijksapps.nl/{organisatie}",
                "secretName": f"{organisatie}-robot-pull-secret",
            },
        )
        ander = self._with_registry("slachtoffer", {"name": "eigen", "upstream": "ghcr.io/team"})
        with self._store(eigen, ander), pytest.raises(ProjectIntegrityError, match="slachtoffer"):
            await validate_project_structure(eigen)

    @pytest.mark.asyncio
    async def test_twee_entries_op_een_organisatie_worden_niet_opgeslagen(self):
        eigen = {
            "name": "eigen",
            "clusters": ["odcn-production"],
            "components": [{"name": "web", "path": "/"}],
            "services": [
                {
                    "name": "image-registries",
                    "config": {
                        "registries": [
                            {"name": "een", "upstream": "ghcr.io/team"},
                            {"name": "twee", "upstream": "ghcr.io/team"},
                        ]
                    },
                }
            ],
        }
        with self._store(eigen), pytest.raises(ProjectIntegrityError, match="twee"):
            await validate_project_structure(eigen)

    @pytest.mark.asyncio
    async def test_een_eigen_registry_komt_er_gewoon_langs(self):
        """De toegestane tak, zodat de weigering hierboven niet aan iets anders ligt."""
        eigen = self._with_registry("eigen", {"name": "code-overheid", "upstream": "code.overheid.nl/robbert"})
        ander = self._with_registry("ander", {"name": "eigen", "upstream": "ghcr.io/team"})
        with self._store(eigen, ander):
            await validate_project_structure(eigen)
