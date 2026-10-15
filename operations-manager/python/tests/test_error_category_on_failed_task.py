"""Een gefaalde taak zegt van wie de fout is (zad-cli, punt 26).

``error_type`` is een vrije string die op een stuk of twintig plekken los wordt ingevuld.
Een client mag die niet interpreteren: doet hij dat wel, dan heeft hij een tabel die
zwijgt op de dag dat er een waarde bij komt. Het gevolg was dat een gewone invoerfout
("die dienst moet eerst op projectniveau gekozen worden") bij de CLI uitkwam als "niet toe
te schrijven" in plaats van als "jouw aanroep klopte niet".

Daarom staat er nu een ``error_category`` naast, uit de bestaande gesloten ``ErrorCategory``.
De vertaling gebeurt op één plek, waar een taakrecord een antwoord wordt, zodat elk taaktype
hem krijgt en een nieuw taaktype hem niet kan vergeten.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from opi.api.task_models import TASK_RESULT_MODELS, task_response_from_dict
from opi.api.v2.models import ErrorCategory, error_category_for


def _failed(error_type: str | None, **extra: object) -> dict:
    return task_response_from_dict(
        {
            "task_id": "t",
            "task_type": "add_component",
            "status": "failed",
            "result": {"status": "failed", "error": "iets ging mis", "error_type": error_type, **extra},
            "created_at": "2026-08-17T00:00:00Z",
        }
    )["result"]


class TestDeVertaling:
    @pytest.mark.parametrize(
        "error_type",
        ["invalid_services", "invalid_component_references", "validation_error", "not_found", "ambiguous_cluster"],
    )
    def test_een_invoerfout_is_van_de_aanroeper(self, error_type: str) -> None:
        assert error_category_for(error_type) == ErrorCategory.InvalidInput

    def test_de_restorebestemming_houdt_zijn_eigen_categorie(self) -> None:
        """``InvalidTarget`` bestond al en betekent iets specifieks: de bestemming die de
        aanroeper opgaf. Die betekenis oprekken naar elke invoerfout zou hem stukmaken."""
        assert error_category_for("invalid_target") == ErrorCategory.InvalidTarget

    @pytest.mark.parametrize("error_type", ["internal_error", "conflict", "no_encryption_key", "iets_nieuws", None])
    def test_wat_niet_toe_te_schrijven_is_blijft_unknown(self, error_type: str | None) -> None:
        """Een categorie is een belofte over toeschrijving. Gokken is erger dan zeggen dat
        we het niet weten, en een `conflict` is bovendien niemands vergissing."""
        assert error_category_for(error_type) == ErrorCategory.Unknown


class TestHetAntwoord:
    def test_de_categorie_staat_naast_het_type(self) -> None:
        result = _failed("invalid_services")
        assert result["error_type"] == "invalid_services"
        assert result["error_category"] == "InvalidInput"

    def test_een_handler_die_zelf_iets_zegt_wint(self) -> None:
        result = _failed("invalid_services", error_category="CrashLoop")
        assert result["error_category"] == "CrashLoop"

    def test_een_geslaagde_taak_krijgt_geen_categorie(self) -> None:
        result = task_response_from_dict(
            {
                "task_id": "t",
                "task_type": "add_component",
                "status": "completed",
                "result": {"status": "completed", "component_name": "api"},
                "created_at": "2026-08-17T00:00:00Z",
            }
        )["result"]
        assert "error_category" not in result

    def test_een_taak_zonder_resultaat_blijft_leeg(self) -> None:
        response = task_response_from_dict(
            {"task_id": "t", "task_type": "add_component", "status": "running", "created_at": "x"}
        )
        assert response["result"] is None


class TestDeSpec:
    def test_elk_resultaatmodel_met_een_type_kent_ook_de_categorie(self) -> None:
        """Anders belooft de spec de categorie op het ene taaktype wel en op het andere
        niet, terwijl de vertaling ze allemaal raakt."""
        zonder = [
            model.__name__
            for model in TASK_RESULT_MODELS.values()
            if "error_type" in model.model_fields and "error_category" not in model.model_fields
        ]
        assert not zonder, f"resultaatmodellen met error_type maar zonder error_category: {zonder}"

    def test_de_categorie_is_een_opsomming_in_de_spec(self) -> None:
        """Het punt van dit veld: een gesloten verzameling waar een client een test op kan
        pinnen, in plaats van een vrije string."""
        model = next(m for m in TASK_RESULT_MODELS.values() if "error_category" in m.model_fields)
        schema = model.model_json_schema()
        assert "ErrorCategory" in schema.get("$defs", {})
        assert "InvalidInput" in schema["$defs"]["ErrorCategory"]["enum"]


class TestElkeTaakVertelt:
    """26b: het veld declareren is niet genoeg als de runtime het niet invult.

    Drie lagen, en ze moesten alle drie mee. Deze klasse bewaakt de eerste twee; de derde
    (een handler die gooit) staat in tests/test_task_worker_failure_result.py.
    """

    def test_elk_resultaatmodel_kent_type_en_categorie(self) -> None:
        """Ook de taaktypen die vroeger geen faalvelden hadden. Een client die op één
        taaktype op het veld kan rekenen, moet dat op alle kunnen."""
        zonder = [
            model.__name__
            for model in TASK_RESULT_MODELS.values()
            if not {"error_type", "error_category", "error"} <= set(model.model_fields)
        ]
        assert not zonder, f"resultaatmodellen zonder de faalvelden: {zonder}"

    def test_bijna_elke_faaldict_noemt_een_reden(self) -> None:
        """De tweede laag: een handler die 'failed' teruggeeft moet zeggen waarom.

        De vier uitzonderingen zijn de geneste ``processing``-blokken, die de reden een
        niveau hoger al dragen.
        """
        import re
        from pathlib import Path

        bron = "".join(
            (Path(__file__).parent.parent / "opi" / "core" / naam).read_text()
            for naam in (
                "task_handlers_components.py",
                "task_handlers_project.py",
                "task_handlers_operations.py",
                "task_handlers_deployment.py",
            )
        )
        blokken = re.findall(r'\{[^{}]*"status": "failed"[^{}]*\}', bron, re.DOTALL)
        zonder = [" ".join(b.split()) for b in blokken if "error_type" not in b]
        assert all(b == '{"status": "failed"}' for b in zonder), (
            f"faal-dicts zonder error_type die geen genest processing-blok zijn: {zonder}"
        )

    @pytest.mark.parametrize(
        ("error_type", "verwacht"),
        [
            ("invalid_project_name", ErrorCategory.InvalidInput),
            ("already_exists", ErrorCategory.InvalidInput),
            ("component_not_found", ErrorCategory.InvalidInput),
            ("deployment_not_found", ErrorCategory.InvalidInput),
            ("processing_failed", ErrorCategory.Unknown),
            ("internal_error", ErrorCategory.Unknown),
        ],
    )
    def test_de_nieuwe_redenen_vallen_waar_ze_horen(self, error_type: str, verwacht: ErrorCategory) -> None:
        assert error_category_for(error_type) == verwacht


class TestEenOnbereikbareMailrelayIsEenPlatformfout:
    """RC-261: een uitrol die strandt op de mailrelay stuurde de gebruiker naar zijn project.

    De storing van 13 oktober 2026 (de relay verbande het node-IP en kwam niet meer ready)
    liet elke uitrol met send-email falen op "Cannot connect to host rig-mail-relay...", en
    dat kwam als ``processing_failed`` zonder categorie bij de CLI aan: exit 3, "niet toe te
    schrijven". Er was niets aan het project te repareren.

    Twee helften, en de eerste pint de tweede niet: de connector moet van de transportfout
    een eigen uitzondering maken, en de handlers die een uitrol met ``processing_failed``
    rapporteren moeten die omzetten in een categorie.
    """

    @pytest.mark.asyncio
    async def test_de_connector_maakt_van_een_transportfout_een_eigen_uitzondering(self) -> None:
        from opi.connectors.mail import MailConnector, MailRelayUnavailableError

        connector = MailConnector("http://rig-mail-relay:8080", "admin", "geheim")
        fout = aiohttp.ClientConnectorError(
            connection_key=SimpleNamespace(ssl=None, host="rig-mail-relay", port=8080, is_ssl=False),  # type: ignore[arg-type]
            os_error=OSError("Connection refused"),
        )

        # Budget op 0: anders slaapt de herhaallus 30 seconden in deze test.
        with (
            patch.object(MailConnector, "_eenmalig_verzoek", AsyncMock(side_effect=fout)),
            patch("opi.connectors.mail.RELAY_CONNECT_RETRY_SECONDS", 0.0),
            pytest.raises(MailRelayUnavailableError) as gevangen,
        ):
            await connector.get_principal("project-demo")

        tekst = str(gevangen.value)
        assert "mailservice van het platform" in tekst
        assert "niet aan je project" in tekst

    @pytest.mark.parametrize("error_type", ["processing_failed"])
    def test_processing_failed_blijft_zonder_categorie_onverklaarbaar(self, error_type: str) -> None:
        """De tegenproef op de handlers hieronder: de VERTALING verandert niet. Een component
        dat niet gezond wordt kan nog steeds aan het image van de gebruiker liggen."""
        assert error_category_for(error_type) == ErrorCategory.Unknown

    def test_de_categorie_van_de_handler_overleeft_het_antwoord(self) -> None:
        result = _failed("processing_failed", error_category="InternalError")
        assert result["error_category"] == "InternalError"


def _relayfout() -> Exception:
    from opi.connectors.mail import MailRelayUnavailableError

    return MailRelayUnavailableError(
        "De mailservice van het platform is niet bereikbaar, dit ligt niet aan je project "
        "(GET /api/principal/project-demo: Cannot connect to host rig-mail-relay:8080)"
    )


def _gestrande_manager(fout: Exception | None) -> MagicMock:
    """Een ProjectManager die faalde, met ``fout`` als oorspronkelijke uitzondering."""
    manager = MagicMock()
    manager.process_project_from_git = AsyncMock(return_value=False)
    manager.get_processing_error = MagicMock(return_value=str(fout) if fout else "Een component werd niet gezond")
    manager.get_processing_exception = MagicMock(return_value=fout)
    manager.get_component_failures = MagicMock(return_value=None)
    manager.get_terminated_sync_operations = MagicMock(return_value=[])
    manager.save_and_commit_project = AsyncMock(return_value=True)
    manager.close = AsyncMock(return_value=None)
    return manager


def _store() -> MagicMock:
    store = MagicMock()
    store.reconcile = AsyncMock(return_value=None)
    store.get = MagicMock(return_value=SimpleNamespace(filename="demo.yaml"))
    store.read_path = AsyncMock(return_value=None)
    store.read_version = AsyncMock(return_value=None)
    return store


async def _refresh_project(fout: Exception | None) -> dict:
    from opi.core.task_handlers_operations import handle_refresh_project

    manager = _gestrande_manager(fout)
    with (
        patch("opi.core.task_handlers_operations.get_project_store", return_value=_store()),
        patch("opi.manager.project_manager.create_project_manager", return_value=manager),
    ):
        return await handle_refresh_project({"project_name": "demo"}, MagicMock())


async def _refresh_deployment(fout: Exception | None) -> dict:
    from opi.core.task_handlers_operations import handle_refresh_deployment

    manager = _gestrande_manager(fout)
    with (
        patch("opi.core.task_handlers_operations.get_project_store", return_value=_store()),
        patch("opi.manager.project_manager.create_project_manager", return_value=manager),
    ):
        return await handle_refresh_deployment(
            {"project_name": "demo", "deployment_name": "prod"},
            MagicMock(),
        )


async def _create_project(fout: Exception | None) -> dict:
    from opi.core.task_handlers_project import handle_create_project

    manager = _gestrande_manager(fout)
    with (
        patch("opi.services.project_store.get_project_store", return_value=_store()),
        patch("opi.manager.project_manager.ProjectManager", return_value=manager),
        patch("opi.utils.project_utils.validate_project_name", return_value=True),
    ):
        return await handle_create_project(
            {
                "project_name": "demo",
                "yaml_content": "name: demo\ndeployments:\n  - name: prod\n",
                "is_new_project": False,
            },
            MagicMock(),
        )


async def _delete_component(fout: Exception | None) -> dict:
    """De component-delete-handler roept `handle_refresh_project` INTERN aan en bouwt zijn
    eigen antwoord uit het resultaat. Die geeft `error_type` door; zonder de categorie leidt
    `_with_error_category` hem opnieuw af uit "processing_failed" en is hij weer Unknown."""
    from opi.core.task_handlers_components import handle_delete_component

    binnen = await _refresh_project(fout)
    manager = MagicMock()
    manager.delete_component = AsyncMock(return_value={"success": True, "uncoupled_from": []})
    manager.close = AsyncMock(return_value=None)
    with (
        patch("opi.manager.project_manager.ProjectManager", return_value=manager),
        patch("opi.core.task_handlers_operations.handle_refresh_project", AsyncMock(return_value=binnen)),
    ):
        return await handle_delete_component({"project_name": "demo", "component_name": "api"}, MagicMock())


HANDLERS = [
    ("refresh_project", _refresh_project),
    ("refresh_deployment", _refresh_deployment),
    ("create_project", _create_project),
    ("delete_component", _delete_component),
]


class TestDeHandlersMetProcessingFailedSchrijvenDeMailrelayAanHetPlatformToe:
    """Elke handler die een uitrol met ``processing_failed`` rapporteert bouwt zijn eigen
    faal-dict, en HANDLERS somt ze op.

    De overige verwerkingspaden zetten bij een mislukte uitrol geen ``error_type`` en
    krijgen daarom helemaal geen categorie; dat gat staat als eigen taak op PR #236.

    Een toets op ``platform_failure_fields`` alleen zou nul rood geven op de handler die de
    aanroep niet doet, want ``processing_failed`` is daar al de uitkomst.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("naam", "handler"), HANDLERS, ids=[h[0] for h in HANDLERS])
    async def test_een_onbereikbare_relay_wordt_internalerror(self, naam: str, handler: object) -> None:
        resultaat = await handler(_relayfout())  # type: ignore[operator]

        assert resultaat["error_type"] == "processing_failed"
        assert resultaat["error_category"] == ErrorCategory.InternalError.value
        # De reden staat bij de uitrolhandlers in "error" en bij delete_component in
        # "message": dat is de vorm die die handlers al hadden.
        assert "niet aan je project" in (resultaat.get("error") or resultaat.get("message") or "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("naam", "handler"), HANDLERS, ids=[h[0] for h in HANDLERS])
    async def test_een_gewone_mislukking_blijft_onverklaarbaar(self, naam: str, handler: object) -> None:
        """De tegenproef: een component dat niet gezond wordt kan aan het image van de
        gebruiker liggen, dus daar is "niet toe te schrijven" de eerlijke uitkomst."""
        resultaat = await handler(None)  # type: ignore[operator]

        assert resultaat["error_type"] == "processing_failed"
        assert "error_category" not in resultaat
