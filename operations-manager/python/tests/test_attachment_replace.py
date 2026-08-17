"""Een bestaande bijlage vervangen: de id blijft, de inhoud gaat eroverheen.

De reden dat vervangen een eigen bewerking is en geen tweede upload: weggooien en opnieuw
uploaden verbreekt elke koppeling. Elk component dat de bijlage gebruikte wijst daarna
nergens meer heen. Wat hier bewaakt wordt is precies dat verschil, in drie lagen:

* de samenvoeging (``merge_staged_attachments``) -- schrijft over de bestaande regel heen
  in plaats van er een tweede naast te zetten;
* de opslag (``apply_modal_edit``, de echte opslagweg van de bewerkdialoog) -- na een
  vervanging is er een catalogusregel, staat de nieuwe inhoud erin, wijst de koppeling van
  het component er nog steeds naar, en is het realm-wachtwoord dat de sessieredactie
  wegstreepte er nog;
* de poort (``/attachments/stage`` en ``/attachments/validate-id``) -- welke id vervangen
  mag worden beslist de SESSIE, niet het verzoek. Dat de knop de id vastzet is geen
  beveiliging.
"""

from __future__ import annotations

import copy
import io
import shutil
import subprocess
from typing import Any

import pytest
from fastapi import UploadFile
from opi.forms.visualizers.flows import get_flow
from opi.forms.wizard.save import apply_modal_edit
from opi.forms.wizard.secrets import reachable_leaf_keys, redact_unreachable_secrets
from opi.forms.wizard.session import save_modal_state_by_token
from opi.forms.wizard.state import WizardState
from opi.handlers.project_file_handler import (
    extract_attachment_catalog,
    extract_component_attachment_uses,
    merge_staged_attachments,
)
from opi.services import upload_staging
from opi.utils.age import decrypt_age_block_to_bytes_sync
from opi.web.router_wizard import _split_data_across_sections
from opi.web.router_wizard_attachments import (
    REPLACE_TARGET_KEY,
    stage_attachment,
    validate_attachment_id,
)
from starlette.requests import Request

PROJECT_NAME = "bijlage-vervangen"
FLOW_ID = "modal-edit-attachments"
TOKEN = "0" * 32

OLD_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\noud\n-----END AGE ENCRYPTED FILE-----\n"
REALM_PASSWORD = "-----BEGIN AGE ENCRYPTED FILE-----\nrealm\n-----END AGE ENCRYPTED FILE-----\n"


# --------------------------------------------------------------------------------------
# 1. De samenvoeging
# --------------------------------------------------------------------------------------


def _catalog(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"services": [{"attachments": {"data": list(entries)}}]}


def test_een_vervanging_schrijft_over_de_bestaande_regel_heen() -> None:
    yaml_data = _catalog(
        {"id": "server-cert", "filename": "oud.pem", "content": OLD_BLOCK},
        {"id": "ca", "filename": "ca.pem", "content": OLD_BLOCK},
    )

    merge_staged_attachments(
        yaml_data,
        {"server-cert": {"filename": "nieuw.pem", "content": "staging:abc", "replace": True}},
    )

    data = yaml_data["services"][0]["attachments"]["data"]
    # Geen tweede regel, en de regel staat nog op zijn eigen plek: de catalogus is
    # ongewijzigd op de INHOUD van die ene bijlage na.
    assert [entry["id"] for entry in data] == ["server-cert", "ca"]
    assert data[0] == {"id": "server-cert", "filename": "nieuw.pem", "content": "staging:abc"}


def test_zonder_de_vervangvlag_blijft_de_opgeslagen_inhoud_staan() -> None:
    """Een botsing die niet als vervanging binnenkwam overschrijft niets.

    De vlag wordt gezet door de weg die de id tegen de sessie toetst. Raakt hij onderweg
    kwijt, dan is stil overschrijven de ergste afloop: dan gaat er inhoud weg die niemand
    heeft aangewezen.
    """
    yaml_data = _catalog({"id": "server-cert", "filename": "oud.pem", "content": OLD_BLOCK})

    merge_staged_attachments(yaml_data, {"server-cert": {"filename": "nieuw.pem", "content": "staging:abc"}})

    data = yaml_data["services"][0]["attachments"]["data"]
    assert data == [{"id": "server-cert", "filename": "oud.pem", "content": OLD_BLOCK}]


def test_een_nieuwe_id_komt_er_gewoon_bij() -> None:
    yaml_data = _catalog({"id": "server-cert", "filename": "oud.pem", "content": OLD_BLOCK})

    merge_staged_attachments(yaml_data, {"ca": {"filename": "ca.pem", "content": "staging:def"}})

    assert [entry["id"] for entry in yaml_data["services"][0]["attachments"]["data"]] == ["server-cert", "ca"]


# --------------------------------------------------------------------------------------
# 2. De opslag: dezelfde weg als de bewerkdialoog
# --------------------------------------------------------------------------------------

_age_available = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age/age-keygen binary not available",
)


@pytest.fixture
def age_keypair() -> tuple[str, str]:
    result = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines() + result.stderr.splitlines()
    private_key = next(line for line in lines if line.startswith("AGE-SECRET-KEY"))
    public_key = next(line.split(": ", 1)[1].strip() for line in lines if "public key:" in line.lower())
    return public_key, private_key


def _project(public_key: str) -> dict[str, Any]:
    """Een project met een gekoppelde bijlage EN een realm-wachtwoord.

    De twee horen in hetzelfde bestand: de bijlage moet de sessieredactie overleven, en het
    wachtwoord is de bekende buurman die daarbij sneuvelde (RC-102).
    """
    return {
        "schema-version": 2,
        "name": PROJECT_NAME,
        "display-name": "Bijlage vervangen",
        "users": [{"email": "admin@example.com", "role": "admin"}],
        "clusters": ["sandboxed-local"],
        # De catalogus staat in de naam-als-sleutel vorm: normalize_service_entries laat
        # attachments daar met opzet in staan (zie _normalize_service_entry), dus dit is
        # de vorm die een opgeslagen project echt draagt.
        "services": [
            {"attachments": {"data": [{"id": "server-cert", "filename": "oud.pem", "content": OLD_BLOCK}]}},
            "keycloak",
        ],
        "components": [
            {
                "name": "backend",
                "type": "single",
                "ports": {"inbound": [8000]},
                "services": [
                    {
                        "reference": "attachments",
                        "config": [{"reference": "server-cert", "provide-as": "file", "path": "/etc/ssl/server.pem"}],
                    }
                ],
            }
        ],
        "config": {
            "age-public-key": public_key,
            "keycloak": [
                {
                    "host": "https://keycloak.example.invalid",
                    "realm": f"{PROJECT_NAME}-sandboxed-local",
                    "username": "admin",
                    "password": REALM_PASSWORD,
                }
            ],
        },
    }


async def _save_with_replacement(project_data: dict[str, Any], staged: dict[str, Any]) -> dict[str, Any]:
    """Sla op zoals de bewerkdialoog dat doet, met deze bestanden gestaged.

    Dezelfde stappen als ``modal_wizard_init`` + ``apply_modal_edit``: de sessie draagt een
    GEREDIGEERDE kopie (dat is waar de encrypted waarden sneuvelen) en de opslag werkt op
    het project zoals dat vers uit git komt.
    """
    flow = get_flow(FLOW_ID)
    keep = reachable_leaf_keys([ed for section in flow.sections for ed in section.editables])
    session_data, _ = redact_unreachable_secrets(copy.deepcopy(project_data), keep)

    state = WizardState(flow_id=FLOW_ID, current_step=flow.sections[0].section_id, project_name=PROJECT_NAME)
    state.step_data = _split_data_across_sections(flow, session_data)
    state.active_sections = [section.section_id for section in flow.sections]
    state.populate_virt_mappings(flow.sections)
    state.base_data = copy.deepcopy(session_data)
    state.staged_attachments = staged

    original_content = {
        att_id: entry.get("content")
        for att_id, entry in extract_attachment_catalog(project_data).items()
        if entry.get("content")
    }
    return await apply_modal_edit(
        copy.deepcopy(project_data),
        state.get_merged_data(strip_cleared=False),
        flow=flow,
        active_sections=list(flow.sections),
        state=state,
        project_name=PROJECT_NAME,
        original_attachment_content=original_content,
    )


@_age_available
@pytest.mark.asyncio
async def test_vervangen_laat_de_koppelingen_staan(age_keypair: tuple[str, str]) -> None:
    """De toets die de reden van deze bewerking bewaakt.

    Verwijderen-en-opnieuw-uploaden verliest de koppeling; vervangen niet. Daarom staat
    hier niet alleen dat de inhoud nieuw is, maar ook dat het component nog steeds naar
    dezelfde id wijst -- en dat er precies EEN catalogusregel is, want een tweede regel met
    dezelfde id is een project dat niemand meer kan lezen.
    """
    public_key, private_key = age_keypair
    project_data = _project(public_key)
    nieuwe_inhoud = b"-----BEGIN CERTIFICATE-----\nNIEUW\n-----END CERTIFICATE-----\n"
    token = upload_staging.stage_file(nieuwe_inhoud, "nieuw.pem")

    saved = await _save_with_replacement(
        project_data,
        {"server-cert": {"filename": "nieuw.pem", "content": f"staging:{token}", "replace": True}},
    )

    catalog = extract_attachment_catalog(saved)
    assert list(catalog) == ["server-cert"]
    assert catalog["server-cert"]["filename"] == "nieuw.pem"
    assert decrypt_age_block_to_bytes_sync(str(catalog["server-cert"]["content"]), private_key) == nieuwe_inhoud

    uses = extract_component_attachment_uses(saved["components"][0])
    assert [use["reference"] for use in uses] == ["server-cert"]
    assert uses[0]["path"] == "/etc/ssl/server.pem"

    # De buurman: het realm-wachtwoord dat de sessie niet mocht dragen staat er nog.
    assert saved["config"]["keycloak"][0]["password"] == REALM_PASSWORD


@_age_available
@pytest.mark.asyncio
async def test_vervangen_levert_geen_tweede_catalogusregel_op(age_keypair: tuple[str, str]) -> None:
    """Niet via de samenvatting maar op de LIJST zelf: een dubbele id is geen catalogus."""
    public_key, _ = age_keypair
    token = upload_staging.stage_file(b"nieuw", "nieuw.pem")

    saved = await _save_with_replacement(
        _project(public_key),
        {"server-cert": {"filename": "nieuw.pem", "content": f"staging:{token}", "replace": True}},
    )

    data = [
        entry
        for service in saved["services"]
        if isinstance(service, dict) and isinstance(service.get("attachments"), dict)
        for entry in service["attachments"].get("data", [])
    ]
    assert [entry["id"] for entry in data] == ["server-cert"]
    # En geen tweede catalogusblok naast het eerste.
    assert sum(1 for s in saved["services"] if isinstance(s, dict) and "attachments" in s) == 1


# --------------------------------------------------------------------------------------
# 3. De poort: de sessie beslist welke id vervangen wordt
# --------------------------------------------------------------------------------------


def _request() -> Request:
    """Een verzoek zonder wizard in de cookie, zodat het token de sessie aanwijst."""
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "session": {}})


def _session_state(replace_target: str | None, catalog_ids: tuple[str, ...] = ("server-cert",)) -> WizardState:
    state = WizardState(flow_id=FLOW_ID, current_step="attachments", project_name=PROJECT_NAME)
    state.base_data = {
        "services": [{"attachments": {"data": [{"id": att_id, "filename": f"{att_id}.pem"} for att_id in catalog_ids]}}]
    }
    if replace_target:
        state.base_data[REPLACE_TARGET_KEY] = replace_target
    save_modal_state_by_token(TOKEN, state)
    return state


def _upload(name: str = "nieuw.pem") -> UploadFile:
    return UploadFile(file=io.BytesIO(b"inhoud"), filename=name)


async def _stage(attachment_id: str, mode: str) -> str:
    response = await stage_attachment(
        _request(),
        FLOW_ID,
        attachment_id=attachment_id,
        file=_upload(),
        wizard_token=TOKEN,
        mode=mode,
    )
    return response.body.decode()


@pytest.mark.asyncio
async def test_een_andere_id_meesturen_bij_vervangen_wordt_geweigerd() -> None:
    """De knop zet de id vast; dit is de toets dat dat niet de beveiliging is."""
    _session_state("server-cert", catalog_ids=("server-cert", "ca"))

    body = await _stage("ca", mode="replace")

    assert "vast op &#39;server-cert&#39;" in body


@pytest.mark.asyncio
async def test_vervangen_zonder_dat_de_sessie_daarvoor_geopend_is_wordt_geweigerd() -> None:
    _session_state(None)

    body = await _stage("server-cert", mode="replace")

    assert "geen vervanging" in body


@pytest.mark.asyncio
async def test_een_vervanging_van_een_niet_bestaande_bijlage_geeft_een_nette_fout() -> None:
    """De sessie wijst een id aan die niet (meer) in de catalogus staat."""
    _session_state("weg-cert", catalog_ids=("server-cert",))

    body = await _stage("weg-cert", mode="replace")

    assert "bestaat niet in dit project" in body


@pytest.mark.asyncio
async def test_een_bestaande_id_bij_toevoegen_wordt_nog_steeds_geweigerd() -> None:
    """De andere kant op: zonder vervangmodus is een bezette id nog steeds bezet."""
    _session_state(None)

    body = await _stage("server-cert", mode="")

    assert "nog niet opgeslagen" not in body, "de upload is toch gestaged"
    assert "bestaat al" in body


@pytest.mark.asyncio
async def test_de_idcontrole_kent_het_verschil() -> None:
    """Beide kanten op, op het veld zelf: nieuw bij vervangen, bestaand bij toevoegen."""
    _session_state("server-cert")

    vervangen = await validate_attachment_id(
        _request(), FLOW_ID, attachment_id="nog-niet-bestaand", wizard_token=TOKEN, mode="replace"
    )
    toevoegen = await validate_attachment_id(
        _request(), FLOW_ID, attachment_id="server-cert", wizard_token=TOKEN, mode=""
    )

    assert "vast op &#39;server-cert&#39;" in vervangen.body.decode()
    assert "bestaat al" in toevoegen.body.decode()


@pytest.mark.asyncio
async def test_de_samenvatting_toont_het_vervangende_bestand() -> None:
    """De laatste stap voor het opslaan mag niet het bestand tonen dat verdwijnt.

    De samenvatting leest de catalogus, en die draagt bij een vervanging nog de OUDE naam
    -- de nieuwe zit in de sessie. Zonder deze paring bevestigt de gebruiker "oud.pem"
    terwijl hij op het punt staat dat bestand te overschrijven.
    """
    from opi.web.router_detail_edit import _attachment_review_items

    state = WizardState(flow_id=FLOW_ID, current_step="attachments", project_name=PROJECT_NAME)
    state.staged_attachments = {"server-cert": {"filename": "nieuw.pem", "content": "staging:abc", "replace": True}}

    items = _attachment_review_items(_catalog({"id": "server-cert", "filename": "oud.pem"}), state)

    assert items == ["nieuw.pem (server-cert, vervangen)"]
