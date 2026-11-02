"""Broncodelinks bij de projectgegevens: het veld ``source-code`` (RC-408).

Een lijst informatieve links naar de code van het project. Het platform doet er niets
mee, en precies dat is wat hier bewaakt wordt: de vorm (alleen https, begrensde lengtes,
geen dubbelen), de weg naar binnen (het formulier) en de weg naar buiten (de pagina),
plus een grendel die zegt dat geen enkele andere laag het veld leest.

Het functionele ``repositories`` is een ANDER veld, met inloggegevens en een branch, en
een deployment hangt eraan. Een test hier die daaraan komt, meet het verkeerde veld.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import TYPE_CHECKING, Any

import opi.web.lotc_fixtures as lotc_fixtures
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.api.invite_routes import invite_router
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.rendered_sequences import GERENDERDE_REEKSEN_VELD
from opi.forms.models.project_file import ProjectFileModel, SourceCodeLinkModel
from opi.forms.visualizers.wizard_sections import IDENTITY_EDIT_SECTION, IDENTITY_SECTION
from opi.web.lotc_router import router as lotc_router
from pydantic import ValidationError
from starlette.middleware.sessions import SessionMiddleware

if TYPE_CHECKING:
    from opi.forms.visualizers.sections import FormSection

OPI_DIR = Path(__file__).resolve().parent.parent / "opi"


def _project(**extra: Any) -> dict[str, Any]:
    return {
        "name": "proef",
        "display-name": "Proefproject",
        "description": "Een proefproject",
        "users": [{"email": "beheerder@voorbeeld.nl", "roles": ["admin"]}],
        **extra,
    }


# ---------------------------------------------------------------------------
# Het model: de vorm van een link
# ---------------------------------------------------------------------------


class TestSourceCodeLinkModel:
    def test_een_geldige_lijst(self) -> None:
        project = ProjectFileModel.model_validate(
            _project(
                **{
                    "source-code": [
                        {"url": "https://github.com/MinBZK/mijn-app", "name": "Applicatie"},
                        {"url": "https://code.overheid.nl/minbzk/mijn-app-infra"},
                    ]
                }
            )
        )

        assert [link.url for link in project.source_code] == [
            "https://github.com/MinBZK/mijn-app",
            "https://code.overheid.nl/minbzk/mijn-app-infra",
        ]
        assert project.source_code[1].name is None

    @pytest.mark.parametrize(
        "url",
        [
            "http://github.com/x/y",
            "javascript:alert(1)",
            "ssh://git@github.com/x/y",
            "git@github.com:x/y.git",
            "https://github.com/x/y met spatie",
            "https://" + "a" * 500,
        ],
        ids=["http", "javascript", "ssh", "scp", "spatie", "te-lang"],
    )
    def test_een_url_die_geen_klikbare_https_link_is(self, url: str) -> None:
        with pytest.raises(ValidationError):
            SourceCodeLinkModel.model_validate({"url": url})

    def test_een_naam_is_begrensd(self) -> None:
        with pytest.raises(ValidationError):
            SourceCodeLinkModel.model_validate({"url": "https://github.com/x/y", "name": "n" * 101})

    def test_een_onbekend_veld_hoort_bij_repositories_en_niet_hier(self) -> None:
        with pytest.raises(ValidationError):
            SourceCodeLinkModel.model_validate({"url": "https://github.com/x/y", "branch": "main"})

    def test_meer_dan_twintig_links(self) -> None:
        with pytest.raises(ValidationError):
            ProjectFileModel.model_validate(
                _project(**{"source-code": [{"url": f"https://github.com/x/{i}"} for i in range(21)]})
            )

    def test_twee_keer_dezelfde_link(self) -> None:
        with pytest.raises(ValidationError, match="Dubbele broncodelinks"):
            ProjectFileModel.model_validate(
                _project(
                    **{
                        "source-code": [
                            {"url": "https://github.com/x/y", "name": "Een"},
                            {"url": "https://github.com/x/y", "name": "Twee"},
                        ]
                    }
                )
            )


# ---------------------------------------------------------------------------
# Het formulier: wat modal-edit-identity wegschrijft
# ---------------------------------------------------------------------------


async def _opslaan(project: dict[str, Any], links: list[dict[str, Any]]) -> dict[str, Any]:
    """Dien de identiteitssectie in zoals de browser hem stuurt."""
    submitted = {
        "display-name": project["display-name"],
        "description": project["description"],
        "source-code": copy.deepcopy(links),
        GERENDERDE_REEKSEN_VELD: ["source-code"],
    }
    result, errors = await EditableFormProcessor().process_json_submission(
        submitted,
        IDENTITY_EDIT_SECTION.editables,
        copy.deepcopy(project),
        edit_mode=True,
    )
    assert errors == {}, f"onverwachte validatiefouten: {errors}"
    return result


REPOSITORIES = [
    {
        "name": "main-repo",
        "url": "https://github.com/x/y.git",
        "username": "git",
        "branch": "main",
        "path": ".",
    }
]


@pytest.mark.asyncio
async def test_opslaan_schrijft_source_code_in_het_projectbestand() -> None:
    result = await _opslaan(
        _project(repositories=copy.deepcopy(REPOSITORIES)),
        [
            {"url": "https://github.com/MinBZK/mijn-app", "name": "Applicatie"},
            {"url": "https://code.overheid.nl/minbzk/mijn-app-infra"},
        ],
    )

    assert result["source-code"] == [
        {"url": "https://github.com/MinBZK/mijn-app", "name": "Applicatie"},
        {"url": "https://code.overheid.nl/minbzk/mijn-app-infra"},
    ]


@pytest.mark.asyncio
async def test_leegmaken_haalt_het_veld_weg_in_plaats_van_een_lege_lijst_te_schrijven() -> None:
    project = _project(**{"source-code": [{"url": "https://github.com/x/y"}]})

    result = await _opslaan(project, [])

    assert "source-code" not in result


@pytest.mark.asyncio
async def test_repositories_blijft_ongemoeid() -> None:
    """Het functionele veld is een ander veld, en deze sectie gaat er niet over."""
    project = _project(repositories=copy.deepcopy(REPOSITORIES))

    result = await _opslaan(project, [{"url": "https://github.com/MinBZK/mijn-app"}])

    assert result["repositories"] == REPOSITORIES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "section",
    [IDENTITY_SECTION, IDENTITY_EDIT_SECTION],
    ids=["wizard", "modal-edit-identity"],
)
async def test_dezelfde_url_met_een_andere_naam_wordt_geweigerd(section: FormSection) -> None:
    """Het gat dat ``uniqueItems`` in het schema openlaat, op beide schrijfwegen.

    De veldvalidatie ziet twee geldige links, dus ``process_json_submission`` geeft hier
    geen fout; de sectie-enforcer is de laag die ze naast elkaar legt.
    """
    yaml_data = _project(
        **{
            "source-code": [
                {"url": "https://github.com/x/y", "name": "Een"},
                {"url": "https://github.com/x/y", "name": "Twee"},
            ]
        }
    )
    field_errors: dict[str, list[str]] = {}

    global_errors = await EditableFormProcessor().enforce_sections(yaml_data, [section], field_errors=field_errors)

    assert global_errors == []
    assert field_errors == {"source-code[1]/url": ["Deze link staat er al."]}


@pytest.mark.asyncio
async def test_twee_verschillende_links_komen_langs_de_enforcer() -> None:
    yaml_data = _project(
        **{
            "source-code": [
                {"url": "https://github.com/x/y"},
                {"url": "https://github.com/x/z", "name": "Infra"},
            ]
        }
    )
    field_errors: dict[str, list[str]] = {}

    global_errors = await EditableFormProcessor().enforce_sections(
        yaml_data, [IDENTITY_EDIT_SECTION], field_errors=field_errors
    )

    assert (global_errors, field_errors) == ([], {})


# ---------------------------------------------------------------------------
# De weergave op de projectpagina
# ---------------------------------------------------------------------------


def test_build_project_details_neemt_source_code_over_uit_het_projectbestand() -> None:
    """Dezelfde functie als de pagina gebruikt (zie het docstring-contract erin), ongemockt."""
    links = [{"url": "https://github.com/MinBZK/mijn-app", "name": "Applicatie"}]

    details = lotc_fixtures.build_project_details(_project(**{"source-code": links}))

    assert details["source_code"] == links


def test_build_project_details_zonder_source_code_is_een_lege_lijst() -> None:
    assert lotc_fixtures.build_project_details(_project())["source_code"] == []


def _projectpagina(monkeypatch: pytest.MonkeyPatch, links: list[dict[str, Any]] | None) -> str:
    """De overzichtstab van de proefopstelling, met zelf gekozen broncodelinks."""
    origineel = lotc_fixtures.build_project_details

    def met_links(project_data: dict[str, Any]) -> dict[str, Any]:
        details = origineel(project_data)
        details["source_code"] = links if links is not None else []
        return details

    monkeypatch.setattr(lotc_fixtures, "build_project_details", met_links)
    app = FastAPI()
    app.include_router(lotc_router)
    app.include_router(invite_router)
    app.add_middleware(SessionMiddleware, secret_key="test")
    return TestClient(app).get("/lotc/bg/project-tabs?tab=project").text


def test_de_pagina_toont_elke_link_in_een_nieuw_tabblad(monkeypatch: pytest.MonkeyPatch) -> None:
    html = _projectpagina(
        monkeypatch,
        [
            {"url": "https://github.com/MinBZK/mijn-app", "name": "Applicatie"},
            {"url": "https://code.overheid.nl/minbzk/mijn-app-infra"},
        ],
    )

    assert 'href="https://github.com/MinBZK/mijn-app"' in html
    assert 'href="https://code.overheid.nl/minbzk/mijn-app-infra"' in html
    assert html.count('rel="noopener noreferrer"') >= 2
    assert html.count('target="_blank"') >= 2


def test_zonder_naam_staat_de_url_er_als_tekst(monkeypatch: pytest.MonkeyPatch) -> None:
    html = _projectpagina(monkeypatch, [{"url": "https://code.overheid.nl/minbzk/mijn-app-infra"}])

    assert 'text="https://code.overheid.nl/minbzk/mijn-app-infra"' in html


def test_zonder_links_staat_er_geen_lege_lijst(monkeypatch: pytest.MonkeyPatch) -> None:
    html = _projectpagina(monkeypatch, [])

    assert "<ul>\n</ul>" not in html
    assert "noopener noreferrer" not in html


def test_een_naam_met_markup_wordt_ontsmet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autoescape staat aan, dus de naam komt als tekst binnen en niet als element."""
    html = _projectpagina(
        monkeypatch,
        [{"url": "https://github.com/x/y", "name": "<script>alert(1)</script>"}],
    )

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ---------------------------------------------------------------------------
# De grendel: het veld blijft informatief
# ---------------------------------------------------------------------------

#: Modules die ``source-code`` mogen noemen: de vorm, het formulier, de weergave, de API.
#: Doel: het veld blijft informatief, niemand voert het uit.
LEZERS_VAN_SOURCE_CODE = frozenset(
    {
        "api/v2/router.py",
        "forms/editables/enforcers.py",
        "forms/editables/fields/identity.py",
        "forms/models/project_file.py",
        "forms/visualizers/wizard_sections.py",
        "services/schema_migration.py",
        "web/lotc_fixtures.py",
        "web/router.py",
    }
)


def _modules_die_source_code_noemen() -> set[str]:
    """Modules where 'source-code' appears as a string constant (AST-based, excludes comments)."""
    gevonden: set[str] = set()
    for pad in OPI_DIR.rglob("*.py"):
        boom = ast.parse(pad.read_text(encoding="utf-8"))
        for knoop in ast.walk(boom):
            if isinstance(knoop, ast.Constant) and isinstance(knoop.value, str) and "source-code" in knoop.value:
                gevonden.add(pad.relative_to(OPI_DIR).as_posix())
    return gevonden


def test_alleen_de_vorm_het_formulier_en_de_weergave_lezen_source_code() -> None:
    assert _modules_die_source_code_noemen() == set(LEZERS_VAN_SOURCE_CODE)


@pytest.mark.parametrize("laag", ["manager", "generation", "connectors"])
def test_de_uitvoerende_lagen_raken_het_veld_niet(laag: str) -> None:
    """Geen clone, geen build, geen manifest: het veld hoort de uitvoering nooit te halen."""
    assert not [module for module in _modules_die_source_code_noemen() if module.startswith(f"{laag}/")]
