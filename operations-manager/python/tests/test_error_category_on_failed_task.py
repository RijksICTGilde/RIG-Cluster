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
