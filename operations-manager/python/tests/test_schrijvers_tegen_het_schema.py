"""Wat de CODE naar een projectbestand schrijft, langs de poorten die een save draait.

Waarom dit bestand bestaat
--------------------------
Twee keer is dezelfde fout in productie geland. In juni schreef de registry-code
``registries[].secretName`` terwijl het schema dat veld niet kende (`f071f10d`); dat
blokkeerde stil ALLE deploys van dp-bn7. In de nacht van 17 op 18 augustus schreef de
resource-tuner ``requests`` in een historie-item terwijl ``resource-history-entry`` alleen
``limits`` kende (`c0fc23d1`); dat gaf 25 waarschuwingen in de log en 25 formeel ongeldige
projectbestanden.

Beide keren was de reparatie een schemaregel, en beide keren was de test een MET DE HAND
geschreven voorbeeld van de nieuwe vorm. Zo'n voorbeeld bewijst dat het schema de vorm
toelaat die de testschrijver in gedachten had - niet dat het de vorm toelaat die de code
werkelijk wegschrijft. Precies daar zat het gat: geen enkele test legde het door een
SCHRIJVER geproduceerde resultaat langs ``validate_project_schema``.

Wat hier dus gebeurt: elke test draait de echte schrijver op een basisproject dat aantoonbaar
geldig begint, en legt het RESULTAAT langs de twee poorten die ``ProjectStore._validate``
ook draait -- het JSON-schema en de per-dienst configmodellen. Faalt zo'n test, dan zou de
productie-save dezelfde melding geven.

De derde poort, ``validate_project_structure``, staat hier bewust niet: die is async en leest
clusterconfiguratie, en de fouten die hij vindt (dubbele namen, hangende verwijzingen) zijn
niet de klasse die stil in 25 bestanden landt.

De inventaris van schrijvers -- wie er zijn en wat ze schrijven -- staat in
``tests/test_schrijvers_inventaris.py``, dat ook bewaakt dat er geen nieuwe bij komt zonder
dat iemand ernaar gekeken heeft.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.project_schema import validate_project_schema
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.project_validation import validate_service_configs
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, set_domain_setting
from opi.services.catalog.sleep_mode import service as sleep_service
from opi.services.catalog.sleep_mode import state as sleep_state
from opi.services.catalog.sleep_mode import token as sleep_token
from opi.services.resource_tuning_service import apply_resource_tuning
from opi.services.services_enums import ServiceType

#: Een AGE-blok in de vorm die het schema eist, zodat een schrijver die een versleutelde
#: waarde wegschrijft ook echt de patroon-regel raakt in plaats van er langs te glijden.
AGE_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\nY2lwaGVydGV4dA==\n-----END AGE ENCRYPTED FILE-----"


def _basis_project() -> dict[str, Any]:
    """Een projectbestand dat geldig BEGINT, zodat rood door de schrijver komt en niet door de basis."""
    return {
        "schema-version": 2,
        "name": "schrijvers",
        "description": "Basisproject voor de schrijverstests",
        "clusters": ["odcn-production"],
        "users": [{"email": "admin@rijksoverheid.nl", "role": "admin"}],
        "repositories": [
            {
                "name": "main-repo",
                "url": "ssh://git@host.docker.internal:2222/srv/git/schrijvers.git",
                "branch": "main",
                "path": ".",
            }
        ],
        "services": ["publish-on-web"],
        "components": [
            {
                "name": "api",
                "type": "single",
                "ports": {"inbound": [8080], "outbound": [443]},
                "services": ["publish-on-web"],
                "resources": {
                    "requests": {"memory": "64Mi", "cpu": "50m"},
                    "limits": {"memory": "128Mi", "cpu": "1000m"},
                },
            }
        ],
        "deployments": [
            {
                "name": "productie",
                "cluster": "odcn-production",
                "namespace": "schrijvers",
                "repository": "main-repo",
                "components": [{"reference": "api", "image": "nginx:1.25"}],
            }
        ],
        "config": {"age-public-key": "age1d489e9c48pmwam6603vecp7y29zz9fx5cgpe9uk6cu9l7asfzg9sx5s0tq"},
    }


def poorten(project_data: dict[str, Any]) -> None:
    """Draai de twee synchrone poorten die ``ProjectStore._validate`` ook draait."""
    validate_project_schema(project_data)
    validate_service_configs(project_data)


def test_het_basisproject_is_zelf_geldig() -> None:
    """Zonder deze test meet elke andere test hier de basis in plaats van de schrijver."""
    poorten(_basis_project())


# ---------------------------------------------------------------------------
# De resource-tuner: de schrijver van 18 augustus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_de_tuner_laat_een_geldig_projectbestand_achter() -> None:
    """Dit is het bewijs uit het plan: de tuner-KANT, niet een handgeschreven voorbeeld.

    Haal ``requests`` uit ``resource-history-entry`` in project_v2.json en deze test valt om,
    want het historie-item komt uit de tuner zelf (``deployment_history_entry`` in
    resource_tuning_service.py). Dat is precies de melding die op 18 augustus 01:00 25 keer
    in de productielog stond.
    """
    project = _basis_project()
    connector = AsyncMock()
    # max, avg, oom-kills: geen meetdata plus een OOM-kill, de weg die de tuner de limiet
    # laat verdubbelen en dus zowel limits als requests laat schrijven.
    connector.custom_query.side_effect = [[], [], [{"value": [0, "1"]}]]
    kubectl = MagicMock(
        get_deployment_status=AsyncMock(return_value=None), get_vpa_recommendation=AsyncMock(return_value=None)
    )

    with (
        patch("opi.services.resource_tuning_service.KubectlConnector", return_value=kubectl),
        patch("opi.services.resource_tuning_service.get_min_memory_limit_mi", return_value=25),
        patch("opi.services.resource_tuning_service.get_max_memory_limit_mi", return_value=4096),
        patch("opi.services.resource_tuning_service.get_prefixed_namespace", return_value="rig-prd-schrijvers"),
        patch("opi.services.resource_tuning_service.get_metrics_connector", new=AsyncMock(return_value=connector)),
    ):
        changes, _unchanged = await apply_resource_tuning(project, ProjectFileHandler(), "productie")

    assert changes, "zonder wijziging meet deze test niets"
    history = project["deployments"][0]["components"][0]["resources"]["history"]
    assert "requests" in history[0], "de tuner schrijft requests mee; anders leest een request-only wijziging als no-op"
    poorten(project)


def test_de_tuner_comprimeert_de_historie_tot_iets_geldigs() -> None:
    """``compact_resource_history`` rijdt mee op dezelfde commit en herschrijft de historie."""
    project = _basis_project()
    handler = ProjectFileHandler()
    for i in range(8):
        handler.append_deployment_component_resource_history(
            project,
            "productie",
            "api",
            {
                "timestamp": f"2026-08-1{i}T01:00:00+00:00",
                "limits": {"memory": f"{128 + i}Mi"},
                "requests": {"memory": f"{64 + i}Mi"},
                "source": "auto-tune",
                "reason": "meting",
            },
            max_entries=10,
        )
    poorten(project)

    handler.compact_resource_history(project, max_entries=3)

    poorten(project)


# ---------------------------------------------------------------------------
# De oom-watcher en de sanitizer: uit- en weer aanzetten van een component
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("disabled", [True, False], ids=["uit", "weer-aan"])
def test_het_uitzetten_van_een_deploymentcomponent_blijft_geldig(disabled: bool) -> None:
    """De oom-watcher (image-pull) en de sanitizer schrijven allebei via deze zetter."""
    project = _basis_project()

    ProjectFileHandler().set_deployment_component_disabled(
        project, "productie", "api", disabled, "OOMKilled: container api" if disabled else ""
    )

    poorten(project)


def test_het_uitzetten_van_een_component_blijft_geldig() -> None:
    """Dezelfde zetter op componentniveau; die schrijft in een ander deel van het bestand."""
    project = _basis_project()

    ProjectFileHandler().set_component_disabled(project, "api", True, "ImagePullBackOff")

    poorten(project)


def test_het_weer_aanzetten_na_een_nieuwe_image_blijft_geldig() -> None:
    """De deployhaak zet componenten weer aan zodra hun image verandert."""
    project = _basis_project()
    handler = ProjectFileHandler()
    handler.set_deployment_component_disabled(project, "productie", "api", True, "ImagePullBackOff: nginx:1.24")

    handler.reenable_components_with_changed_image(project, ["productie"], force_reenable=True)

    poorten(project)


def test_een_handmatig_gezette_waarde_blijft_geldig() -> None:
    """De portal- en API-weg schrijven allebei via ``apply_user_resource_intent``.

    Die schrijft op drie plekken tegelijk: de resources van de wortelcomponent, een
    ``manual``-item in zijn historie (de bron die ``resource-history-entry`` als laatste
    van de drie in zijn enum kreeg en die tot RC-141 door niemand geschreven werd) en het
    WEGHALEN van dezelfde velden uit de deployment-override. Alle drie moeten door de
    poorten van een save komen.
    """
    project = _basis_project()
    project["deployments"][0]["components"][0]["resources"] = {
        "requests": {"memory": "300Mi", "cpu": "32m"},
        "limits": {"memory": "600Mi", "cpu": "1000m"},
    }
    poorten(project)

    gewijzigd = ProjectFileHandler().apply_user_resource_intent(
        project, "api", {"requests_cpu": "50m", "limits_cpu": "1"}, origin="portal"
    )

    assert gewijzigd, "zonder wijziging meet deze test niets"
    poorten(project)


def test_de_wortelcomponent_bijstellen_blijft_geldig() -> None:
    """De OOM-reparatie schrijft ook op de wortelcomponent: resources en een eigen historie."""
    project = _basis_project()
    handler = ProjectFileHandler()

    handler.set_component_resources(
        project, "api", {"limits_memory": "256Mi", "requests_memory": "128Mi", "limits_cpu": "1000m"}
    )
    handler.append_component_resource_history(
        project,
        "api",
        {
            "timestamp": "2026-08-18T01:00:00+00:00",
            "limits": {"memory": "256Mi"},
            "requests": {"memory": "128Mi"},
            "source": "oom-watcher",
            "reason": "OOM kills detected",
        },
    )

    poorten(project)


def test_het_verwijderen_van_verwijzingen_blijft_geldig() -> None:
    """Verwijderen is ook schrijven: wat achterblijft moet geldig zijn, niet alleen wat weggaat."""
    from opi.handlers.project_file_handler import remove_attachment_references, remove_component_references

    project = _basis_project()
    project["deployments"][0]["components"].append({"reference": "api", "image": "nginx:1.25"})

    remove_attachment_references(project, "ca-bundle")
    remove_component_references(project, "api")

    poorten(project)


# ---------------------------------------------------------------------------
# De back-up- en herstelpaden: generatienummers en kloonstatus
# ---------------------------------------------------------------------------


def test_generatienummers_van_de_backuptaken_blijven_geldig() -> None:
    """``backup_tasks`` schrijft na een restore drie soorten generatie in het bestand."""
    project = _basis_project()
    project["services"].append(ServiceType.POSTGRESQL_DATABASE.value)
    project["services"].append(ServiceType.MINIO_STORAGE.value)
    handler = ProjectFileHandler()

    handler.set_database_generation(project, "productie", 3)
    handler.set_bucket_generation(project, "productie", 2)
    handler.set_storage_generation(project, "productie", "api", "data", 1)
    handler.set_deployment_service_generation(project, "productie", ServiceType.MINIO_STORAGE.value, 4)

    poorten(project)


def test_de_kloonstatus_blijft_geldig() -> None:
    """De restore-router schrijft de afronding van een kloon terug in ``clone-from``."""
    project = _basis_project()
    project["deployments"][0]["clone-from"] = {"type": "deployment", "reference": "acceptatie", "mode": "once"}
    poorten(project)

    ProjectFileHandler().set_clone_status(project, "productie", True, "2026-08-18T01:00:00+00:00")

    poorten(project)


# ---------------------------------------------------------------------------
# Slaapstand: OPI-eigen runtime-staat op de deployment
# ---------------------------------------------------------------------------


def test_de_hele_slaapcyclus_blijft_geldig() -> None:
    """De echte overgangen achter elkaar, met een echt gemunt en versleuteld wektoken.

    De drie standen schrijven elk een ander blok, en het token gaat AGE-versleuteld het
    bestand in -- een met de hand verzonnen token zou de patroonregel van het schema niet
    raken en dus niets bewijzen.
    """
    project = _basis_project()
    versleuteld = sleep_token.encrypt(sleep_token.mint(), project)
    nu = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)

    assert sleep_service.to_sleeping(project, "productie", versleuteld)
    poorten(project)

    assert sleep_service.begin_wake(project, "productie", nu, timedelta(minutes=10))
    assert sleep_state.read(project, "productie").wake_token, "het token moet de overgang overleven"
    poorten(project)

    assert sleep_service.to_awake(project, "productie", nu, timedelta(hours=1))
    poorten(project)


# ---------------------------------------------------------------------------
# Webadressen: de domeininstellingen onder de dienst
# ---------------------------------------------------------------------------


def test_elke_domeininstelling_blijft_geldig() -> None:
    """Elke sleutel die ``DomainSetting`` kent, want de lijst groeit en het schema moet mee."""
    project = _basis_project()
    waarden: dict[DomainSetting, Any] = {
        DomainSetting.BASE_DOMAIN: "rijksapps.nl",
        DomainSetting.SUBDOMAIN: "productie",
        DomainSetting.DOMAIN_FORMAT: "deployment-project",
        DomainSetting.ISSUER: "letsencrypt-prod",
        DomainSetting.ROOT_COMPONENT: "api",
        DomainSetting.BARE_DOMAIN_COMPONENT: "api",
    }
    assert set(waarden) == set(DomainSetting), "een nieuwe instelling hoort hier een waarde te krijgen"
    deployment = project["deployments"][0]

    for setting, waarde in waarden.items():
        set_domain_setting(deployment, setting, waarde)

    poorten(project)


# ---------------------------------------------------------------------------
# ProjectManager-schrijvers: registries en bijlagen
# ---------------------------------------------------------------------------


def _manager() -> Any:
    """Een ProjectManager zonder connectors, zoals tests/test_database_schemas_api.py doet."""
    with (
        patch("opi.manager.project_manager.KubectlConnector"),
        patch("opi.handlers.sops.SopsHandler"),
        patch("opi.generation.manifests.ManifestGenerator"),
        patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
        patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
        patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
        patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
        patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
        patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
        patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
    ):
        from opi.manager.project_manager import ProjectManager

        return ProjectManager()


def _bedraad(project: dict[str, Any]) -> tuple[Any, AsyncMock]:
    """Een manager die ``project`` leest en zijn save vasthoudt in plaats van te committen.

    Dat de save gemockt is, is nu juist het punt: dan draait de validatie van de echte
    schrijfweg NIET, en is wat de schrijver aanbood alleen hier nog te meten.
    """
    manager = _manager()
    manager.get_contents = AsyncMock(return_value=project)
    manager.get_name = AsyncMock(return_value=project["name"])
    save = AsyncMock()
    manager.save_and_commit_project = save
    return manager, save


def _aangeboden(save: AsyncMock) -> dict[str, Any]:
    """Wat de schrijver naar de schrijfweg stuurde."""
    assert save.await_args is not None, "de schrijver heeft niets opgeslagen; dan meet deze test niets"
    data: dict[str, Any] = save.await_args.args[0]
    return data


@pytest.mark.asyncio
async def test_een_registry_op_een_bestaand_secret_blijft_geldig() -> None:
    """De schrijver van het juni-gat: ``secretName`` stond niet in het schema."""
    manager, save = _bedraad(_basis_project())

    result = await manager.upsert_registry_by_secret("rcr", "rcr.rijksapps.nl/rig", "rig-robot-pull-secret")

    assert result["success"]
    poorten(_aangeboden(save))


@pytest.mark.asyncio
async def test_een_registry_met_inloggegevens_blijft_geldig() -> None:
    """Het versleutelde wachtwoord moet ook door de AGE-patroonregel komen."""
    manager, save = _bedraad(_basis_project())

    with patch("opi.manager.project_manager.encrypt_age_content", new=AsyncMock(return_value=AGE_BLOCK)):
        result = await manager.upsert_registry_by_credentials("ghcr", "ghcr.io/rijksictgilde", "robot", "geheim")

    assert result["success"]
    poorten(_aangeboden(save))


@pytest.mark.asyncio
async def test_een_bijlage_blijft_geldig() -> None:
    """Bijlagen schrijven op twee plekken: de catalogus onder de dienst en het gebruik op de component."""
    manager, save = _bedraad(_basis_project())

    with patch("opi.manager.project_manager.encrypt_file_to_age_block_sync", return_value=AGE_BLOCK):
        result = await manager.upsert_attachment(
            attachment_id="ca-bundle",
            filename="ca-bundle.crt",
            content=b"-----BEGIN CERTIFICATE-----",
            on_existing="replace",
            on_absent="create",
            component_name="api",
            binding={"provide-as": "file", "path": "/etc/ssl/certs/ca-bundle.crt"},
        )

    assert result["success"], result
    poorten(_aangeboden(save))


# ---------------------------------------------------------------------------
# De schema-migratie: de schrijver die elk bestand aanraakt
# ---------------------------------------------------------------------------


def test_de_migratie_van_een_oud_bestand_levert_iets_geldigs_op() -> None:
    """``migrate_to_latest`` herschrijft elk ingelezen bestand; het resultaat is wat wordt bewaard."""
    from opi.services.schema_migration import migrate_to_latest

    oud = copy.deepcopy(_basis_project())
    oud["schema-version"] = 1

    migrated, _changed = migrate_to_latest(oud)

    poorten(migrated)


# ---------------------------------------------------------------------------
# Mailaccounts: wat de mailmanager onder de dienst send-email wegschrijft
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_een_mailaccount_blijft_geldig() -> None:
    """``mail_manager`` schrijft per cluster een account onder ``send-email``.

    De echte schrijver, niet een met de hand nagebouwd item: het wachtwoord gaat
    AGE-versleuteld het bestand in, en juist die vorm (een blokstring met een patroonregel)
    is wat een zelfverzonnen voorbeeld niet zou raken. Precies de klasse fout waar dit
    bestand voor bestaat.
    """
    from opi.connectors.mail import MailAccount
    from opi.manager.mail_manager import MailManager
    from opi.services.project import Project

    project = _basis_project()
    project["services"].append({"name": ServiceType.SEND_EMAIL.value, "config": {"from-name": "Schrijvers"}})
    poorten(project)

    # Envelope en From: zijn sinds RC-145 hetzelfde adres, en het draagt de PROJECTnaam
    # (niet de accountnaam, die het voorvoegsel project- draagt).
    afzender = bounce = MailManager._sender_address("odcn-production", "schrijvers")
    manager = MailManager(MagicMock(save_and_commit_project=AsyncMock()))
    await manager._store_account(
        Project(project),
        project,
        "schrijvers",
        "odcn-production",
        MailAccount(
            username="project-schrijvers",
            from_address=afzender,
            bounce_address=bounce,
            messages_per_day=500,
        ),
        "een-wachtwoord-dat-versleuteld-wordt",
    )

    poorten(project)
