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
