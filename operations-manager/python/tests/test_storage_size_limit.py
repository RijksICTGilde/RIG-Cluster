"""De bovengrens op opslag: wat er binnenkomt wordt begrensd, wat er staat niet.

Tot nu toe was de keuzelijst van het formulier de enige rem, en een keuzelijst is geen
regel: het size-editable had een ``values_provider`` en geen validator, het JSON-schema
typeerde ``size`` als een kale string, en de config-API keek er niet naar. Een mount van
10Gi liep zo door naar een PVC.

Deze test legt de twee kanten van het besluit vast:

1. Wat een client of formulier INSTUURT wordt begrensd op de grootste maat die we
   aanbieden (``STORAGE_SIZES``).
2. Wat AL IN een projectbestand staat wordt met rust gelaten. Anders wordt een ouder
   project met een grotere mount een bestand dat niet meer op te slaan is, en krimpen
   kan een PVC niet, dus de eigenaar zou er niet eens aan kunnen voldoen.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.forms.editables.validators import StorageSizeValidator
from opi.forms.visualizers.providers import StorageSizeOptionsProvider
from opi.generation.manifests import ManifestGenerator
from opi.manager.project_validation import STORED_PROJECT_CONTEXT
from opi.manager.pvc_manager import PVCManager
from opi.services.catalog.persistent_storage.editables import PERSISTENT_STORAGE_SIZE_EDITABLE
from opi.services.catalog.shared.storage import (
    DEFAULT_STORAGE_SIZE,
    STORAGE_SIZES,
    StorageConfig,
    StorageEntry,
    check_storage_size,
)
from opi.services.catalog.temp_storage.editables import TEMP_STORAGE_SIZE_EDITABLE
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType
from pydantic import ValidationError


def _entry(size: str) -> dict[str, str]:
    return {"name": "data", "size": size, "mount-path": "/data"}


class TestDeRegelZelf:
    @pytest.mark.parametrize("size", STORAGE_SIZES)
    def test_elke_aangeboden_maat_is_toegestaan(self, size: str) -> None:
        """Wat de keuzelijst aanbiedt moet de regel accepteren, anders belooft het
        formulier iets dat de API weigert."""
        assert check_storage_size(size) == size

    def test_een_maat_onder_het_plafond_mag_ook_als_hij_niet_in_de_lijst_staat(self) -> None:
        """De regel is een plafond, geen opsomming: 512Mi schaadt niets en een bestaand
        project dat het gebruikt moet gewoon door de API bewerkt kunnen worden."""
        assert check_storage_size("512Mi") == "512Mi"

    @pytest.mark.parametrize("size", ["10Gi", "2Gi", "1025Mi", "1T"])
    def test_boven_het_plafond_wordt_geweigerd(self, size: str) -> None:
        with pytest.raises(ValueError, match="groter dan het maximum"):
            check_storage_size(size)

    @pytest.mark.parametrize("size", ["banaan", "", "1 gigabyte", "-5Mi"])
    def test_wat_geen_hoeveelheid_is_wordt_geweigerd(self, size: str) -> None:
        with pytest.raises(ValueError, match="Ongeldige opslaggrootte"):
            check_storage_size(size)

    def test_nul_is_geen_maat(self) -> None:
        with pytest.raises(ValueError, match="groter zijn dan nul"):
            check_storage_size("0Mi")


class TestDeApiWeg:
    """Het configmodel typeert de request-bodies van de config-endpoints, dus dit is de
    API-weg: de weigering valt op de grens, voordat er iets is opgeslagen."""

    def test_het_model_weigert_een_te_grote_mount(self) -> None:
        with pytest.raises(ValidationError, match="groter dan het maximum"):
            StorageEntry.model_validate(_entry("10Gi"))

    def test_het_model_noemt_de_beschikbare_maten(self) -> None:
        with pytest.raises(ValidationError, match="50Mi, 100Mi, 250Mi, 500Mi, 1Gi"):
            StorageEntry.model_validate(_entry("20Gi"))

    @pytest.mark.parametrize("service", [ServiceType.PERSISTENT_STORAGE, ServiceType.TEMP_STORAGE])
    def test_beide_opslagdiensten_zitten_achter_dezelfde_grens(self, service: ServiceType) -> None:
        with pytest.raises(ValidationError, match="groter dan het maximum"):
            get_service(service).validate_config([_entry("5Gi")])

    def test_de_grens_staat_in_het_gepubliceerde_schema(self) -> None:
        """Een client leest het schemafragment; daar hoort de grens in te staan in plaats
        van dat hij hem uit een 422 moet afleiden."""
        beschrijving = StorageConfig.model_json_schema()["$defs"]["StorageEntry"]["properties"]["size"]["description"]
        assert "1Gi" in beschrijving
        assert all(size in beschrijving for size in STORAGE_SIZES)


class TestDeFormulierWeg:
    """Het formulier valideert het veld zelf, want de keuzelijst zit alleen in de HTML."""

    @pytest.mark.parametrize("editable", [PERSISTENT_STORAGE_SIZE_EDITABLE, TEMP_STORAGE_SIZE_EDITABLE])
    def test_het_size_veld_heeft_een_validator(self, editable) -> None:
        assert isinstance(editable.validator, StorageSizeValidator)

    def test_een_te_grote_waarde_levert_een_veldfout(self) -> None:
        fouten = StorageSizeValidator().validate("10Gi")
        assert len(fouten) == 1
        assert "groter dan het maximum" in fouten[0]

    def test_een_lege_waarde_is_niet_de_taak_van_deze_validator(self) -> None:
        assert StorageSizeValidator().validate("") == []
        assert StorageSizeValidator().validate(None) == []


class TestBestaandeProjecten:
    def test_een_opgeslagen_mount_boven_het_plafond_blijft_geldig(self) -> None:
        """De poort die op elke opslag en elke reprocess draait mag een bestaand bestand
        niet alsnog afkeuren -- dat is de dp-bn7-fout: een validatiegat dat elke deploy
        van een project stil laat stranden."""
        entry = StorageEntry.model_validate(_entry("10Gi"), context=STORED_PROJECT_CONTEXT)
        assert entry.size == "10Gi"

    @pytest.mark.parametrize("service", [ServiceType.PERSISTENT_STORAGE, ServiceType.TEMP_STORAGE])
    def test_de_bestandspoort_laat_bestaande_maten_staan(self, service: ServiceType) -> None:
        config = get_service(service).validate_config([_entry("10Gi")], context=STORED_PROJECT_CONTEXT)
        assert config.root[0].size == "10Gi"

    def test_de_vorm_wordt_ook_bij_bestaande_data_nog_gecontroleerd(self) -> None:
        """Meegeven dat iets al bestaat zet alleen het plafond opzij, niet de rest."""
        with pytest.raises(ValidationError, match="mount-pad"):
            StorageEntry.model_validate(
                {"name": "data", "size": "1Gi", "mount-path": "/data/../etc"}, context=STORED_PROJECT_CONTEXT
            )


class TestEenLijst:
    def test_de_keuzelijst_en_de_grens_komen_uit_dezelfde_bron(self) -> None:
        """Twee lijsten die uit elkaar lopen is precies hoe een keuzelijst iets anders
        gaat beloven dan de API accepteert."""
        keuzes = [str(option["value"]) for option in StorageSizeOptionsProvider().get_options()]
        assert keuzes == list(STORAGE_SIZES)

    def test_elke_keuze_heeft_een_leesbaar_label(self) -> None:
        for option in StorageSizeOptionsProvider().get_options():
            assert option["label"] != option["value"], f"maat {option['value']} mist een label"


class TestDeTerugvalInDeManifestgeneratie:
    """Een entry zonder ``size`` kan niet uit een gevalideerde schrijfactie komen, want
    het veld is verplicht. Kwam hij er toch, dan werd er stil een PVC van 10Gi gerenderd:
    tien keer de grootste maat die we aanbieden, en een volume dat niemand vroeg."""

    @staticmethod
    def _pvc_manager():
        project_manager = MagicMock()
        project_manager._project_file_handler.get_storage_generation.return_value = 0
        project_manager._project_file_handler.get_storage_backup_enabled.return_value = False
        return PVCManager(project_manager)

    async def _render(self, tmp_path, storage: dict) -> str:
        with (
            patch("opi.core.database_pools.get_database_pool"),
            patch("opi.services.marked_for_deletion_service.MarkedForDeletionService") as svc,
        ):
            svc.return_value.get_marks_for_deployment = AsyncMock(return_value=[])
            await self._pvc_manager().create_pvc_manifests_for_component(
                project_data={"name": "proj"},
                deployment={"name": "deploy-a"},
                component_name="webapp",
                unique_name="deploy-a-webapp",
                persistent_storage=[storage],
                namespace="proj",
                cluster="local",
                full_output_dir=str(tmp_path),
                manifest_generator=ManifestGenerator(),
            )
        return (tmp_path / "webapp-data-pvc.yaml").read_text()

    @pytest.mark.asyncio
    async def test_zonder_maat_valt_hij_terug_op_de_startmaat(self, tmp_path) -> None:
        manifest = await self._render(tmp_path, {"name": "data"})
        assert f"storage: {DEFAULT_STORAGE_SIZE}" in manifest
        assert "10Gi" not in manifest

    @pytest.mark.asyncio
    async def test_een_bestaande_grotere_maat_wordt_niet_gekrompen(self, tmp_path) -> None:
        """De terugval geldt alleen bij een ONTBREKENDE maat. Een opgeslagen mount die
        boven het plafond ligt wordt gerenderd zoals hij is: een PVC kan niet krimpen, dus
        hem hier verkleinen zou de sync laten falen in plaats van iets op te lossen."""
        manifest = await self._render(tmp_path, {"name": "data", "size": "10Gi"})
        assert "storage: 10Gi" in manifest
