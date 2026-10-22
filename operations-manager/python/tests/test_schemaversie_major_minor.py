"""De schemaversie is MAJOR.MINOR met een echte vergelijking: 2.10 is nieuwer dan 2.9.

Zie ``features/project-schema-versions.md``. Als getal zou 2.10 gelijk zijn aan 2.1 en dus
ouder dan 2.9; dat is de reden dat er een versietype is en dat OPI een versie als string
wegschrijft.
"""

import pytest
from opi.core.project_schema import (
    ProjectSchemaError,
    SchemaVersion,
    parse_schema_version,
    version_key,
)
from opi.services import schema_migration
from opi.services.schema_migration import migrate_to_latest


class TestParsen:
    @pytest.mark.parametrize(
        ("waarde", "verwacht"),
        [
            (2, SchemaVersion(2, 0)),
            (1, SchemaVersion(1, 0)),
            (2.9, SchemaVersion(2, 9)),
            (2.1, SchemaVersion(2, 1)),
            ("2", SchemaVersion(2, 0)),
            ("2.9", SchemaVersion(2, 9)),
            ("2.10", SchemaVersion(2, 10)),
            ("2.11", SchemaVersion(2, 11)),
            ("3.0", SchemaVersion(3, 0)),
        ],
    )
    def test_een_getal_of_tekst_wordt_major_minor(self, waarde: int | float | str, verwacht: SchemaVersion) -> None:
        assert parse_schema_version(waarde) == verwacht

    def test_een_float_wordt_via_zijn_tekstvorm_gelezen(self) -> None:
        # 2.10 bestaat niet als float: het IS 2.1. Alleen de string kan (2, 10) zijn.
        assert parse_schema_version(2.10) == SchemaVersion(2, 1)
        assert parse_schema_version("2.10") == SchemaVersion(2, 10)

    def test_een_versietype_gaat_ongewijzigd_door(self) -> None:
        versie = SchemaVersion(2, 10)
        assert parse_schema_version(versie) is versie

    @pytest.mark.parametrize("waarde", ["2.x", "", "2.1.3", "v2.1", -1, -2.5, True, "2,1", " 2.1"])
    def test_een_ongeldige_waarde_wordt_geweigerd(self, waarde: object) -> None:
        with pytest.raises(ProjectSchemaError, match="MAJOR"):
            parse_schema_version(waarde)  # type: ignore[arg-type]

    def test_een_absurd_lange_waarde_geeft_ook_een_schemafout(self) -> None:
        """int() geeft boven 4300 cijfers een ValueError, en die vangt de gate niet."""
        with pytest.raises(ProjectSchemaError, match="MAJOR"):
            parse_schema_version("9" * 5000)


class TestVergelijken:
    def test_2_10_is_nieuwer_dan_2_9(self) -> None:
        assert parse_schema_version("2.10") > parse_schema_version("2.9")

    def test_de_keten_loopt_op(self) -> None:
        keten = [parse_schema_version(v) for v in ("2", "2.1", "2.9", "2.10", "2.11", "3.0")]
        assert keten == sorted(keten)

    def test_sorteren_gaat_niet_alfabetisch(self) -> None:
        versies = [parse_schema_version(v) for v in ("2.9", "2.10", "2.2")]
        assert [version_key(v) for v in sorted(versies)] == ["2.2", "2.9", "2.10"]


class TestCanoniekeTekstvorm:
    @pytest.mark.parametrize(
        ("waarde", "verwacht"),
        [(2, "2"), (2.0, "2"), ("2.0", "2"), (2.9, "2.9"), ("2.10", "2.10"), (1, "1")],
    )
    def test_version_key(self, waarde: int | float | str, verwacht: str) -> None:
        assert version_key(waarde) == verwacht


class TestDeStampIsEenString:
    def test_een_migratie_naar_2_10_schrijft_de_versie_als_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Als getal zou YAML er 2.1 van maken, en dan was het bestand verouderd geraakt."""
        doel = SchemaVersion(2, 10)
        monkeypatch.setattr(schema_migration, "LATEST_SCHEMA_VERSION", doel)
        monkeypatch.setattr(schema_migration, "LATEST_SCHEMA_VERSION_VALUE", version_key(doel))

        resultaat, gemigreerd = migrate_to_latest({"schema-version": 2.9, "name": "voorbeeld"})

        assert gemigreerd is True
        assert resultaat["schema-version"] == "2.10"
        assert parse_schema_version(resultaat["schema-version"]) == doel

    def test_een_bestand_op_de_nieuwste_versie_wordt_niet_herschreven(self) -> None:
        bestaand = schema_migration.LATEST_SCHEMA_VERSION_VALUE
        data = {"schema-version": bestaand, "name": "voorbeeld"}
        resultaat, gemigreerd = migrate_to_latest(data)
        assert gemigreerd is False
        assert resultaat["schema-version"] == bestaand
