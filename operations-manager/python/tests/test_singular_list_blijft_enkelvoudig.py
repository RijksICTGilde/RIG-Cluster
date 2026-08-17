"""Een lijst die de API als één entry toont kan er ook maar één houden (zad-cli, punt 13).

DE MELDING

Twee gewone aanroepen brachten een project in een stand waarin de gewone read weigerde:

    service config patch invite --set 'add[0].key=' --set 'add[0].contact-email=test@...'
    service config patch invite --set 'add[0].key=' --set 'add[0].contact-email=admin@...'
    service config get invite
      -> 409: 'active' of service 'invite' holds 2 entries at target 'project',
              but this API presents it as a single entry.

Allebei de PATCH-aanroepen waren geldig. De eerste verving de bestaande entry met de lege
sleutel; de tweede vond niets om te vervangen omdat de eerste inmiddels een gegenereerde
sleutel had, en voegde dus toe.

DE KEUZE

De gevel bewaken bij de HANDELING die hem onwaar maakt, in plaats van de volgende lezer
ermee op te zadelen. Dat laatste was bovendien een val: de uitweg vraagt welke sleutel je
moet weghalen, en dat is precies wat de read je niet meer vertelt.

Verwijderen blijft daarom altijd toegestaan. Anders zou een bestand dat al twee entries
heeft (met de hand geschreven, of van voor deze grens) geen weg terug hebben.
"""

from __future__ import annotations

import pytest
from opi.services.catalog.base import ConfigLayer
from opi.services.services import ServiceAdapter, ServiceValidationError


def _project(*invites: dict) -> dict:
    return {
        "name": "proj",
        "services": [{"reference": "invite", "config": {"default-language": "nl", "active": list(invites)}}],
    }


def _invite(key: str) -> dict:
    return {"key": key, "contact-email": f"{key}@example.com"}


def _active(project: dict) -> list[dict]:
    return project["services"][0]["config"]["active"]


class TestDeTweedeEntryWordtGeweigerd:
    def test_toevoegen_naast_een_bestaande_uitnodiging_kan_niet(self) -> None:
        project = _project(_invite("eerste"))

        with pytest.raises(ServiceValidationError, match="single entry"):
            ServiceAdapter.patch_service_config_list(
                project, "invite", ConfigLayer.PROJECT, add=[_invite("tweede")], remove=[], list_field="active"
            )

    def test_de_melding_zegt_wat_dan_wel(self) -> None:
        """Een weigering die niet zegt welke weg werkt is een muur."""
        project = _project(_invite("eerste"))

        with pytest.raises(ServiceValidationError, match="Remove the entry that is there"):
            ServiceAdapter.patch_service_config_list(
                project, "invite", ConfigLayer.PROJECT, add=[_invite("tweede")], remove=[], list_field="active"
            )

    def test_het_bestand_blijft_zoals_het_was(self) -> None:
        """Weigeren vóór het schrijven, niet erna."""
        project = _project(_invite("eerste"))

        with pytest.raises(ServiceValidationError):
            ServiceAdapter.patch_service_config_list(
                project, "invite", ConfigLayer.PROJECT, add=[_invite("tweede")], remove=[], list_field="active"
            )

        assert [entry["key"] for entry in _active(project)] == ["eerste"]


class TestWatWelMag:
    def test_de_eerste_uitnodiging(self) -> None:
        project = _project()

        telling = ServiceAdapter.patch_service_config_list(
            project, "invite", ConfigLayer.PROJECT, add=[_invite("eerste")], remove=[], list_field="active"
        )

        assert telling == {"added": 1, "updated": 0, "removed": 0}
        assert [entry["key"] for entry in _active(project)] == ["eerste"]

    def test_vervangen_verandert_het_aantal_niet(self) -> None:
        project = _project(_invite("eerste"))

        telling = ServiceAdapter.patch_service_config_list(
            project,
            "invite",
            ConfigLayer.PROJECT,
            add=[{"key": "eerste", "contact-email": "nieuw@example.com"}],
            remove=[],
            list_field="active",
        )

        assert telling["updated"] == 1
        assert _active(project)[0]["contact-email"] == "nieuw@example.com"

    def test_wisselen_in_een_aanroep(self) -> None:
        """De weg die de melding aanwijst: haal de bestaande weg en zet de nieuwe erbij."""
        project = _project(_invite("eerste"))

        ServiceAdapter.patch_service_config_list(
            project,
            "invite",
            ConfigLayer.PROJECT,
            add=[_invite("tweede")],
            remove=["eerste"],
            list_field="active",
        )

        assert [entry["key"] for entry in _active(project)] == ["tweede"]


class TestEenBestandDatErAlTweeHeeft:
    """Zonder deze uitzondering zou zo'n project op slot zitten."""

    def test_verwijderen_kan_altijd(self) -> None:
        project = _project(_invite("eerste"), _invite("tweede"))

        telling = ServiceAdapter.patch_service_config_list(
            project, "invite", ConfigLayer.PROJECT, add=[], remove=["tweede"], list_field="active"
        )

        assert telling["removed"] == 1
        assert [entry["key"] for entry in _active(project)] == ["eerste"]

    def test_er_nog_een_bij_zetten_niet(self) -> None:
        project = _project(_invite("eerste"), _invite("tweede"))

        with pytest.raises(ServiceValidationError, match="single entry"):
            ServiceAdapter.patch_service_config_list(
                project, "invite", ConfigLayer.PROJECT, add=[_invite("derde")], remove=[], list_field="active"
            )


class TestEenGewoneLijstMerktNiets:
    """De grens geldt alleen waar een dienst zijn lijst als enkelvoud toont."""

    def test_storage_houdt_meerdere_mounts(self) -> None:
        project = {
            "name": "proj",
            "components": [
                {
                    "name": "backend",
                    "type": "single",
                    "services": [
                        {
                            "reference": "persistent-storage",
                            "config": [{"name": "data1", "size": "1Gi", "mount-path": "/data1"}],
                        }
                    ],
                }
            ],
        }

        telling = ServiceAdapter.patch_service_config_list(
            project,
            "persistent-storage",
            ConfigLayer.COMPONENT,
            add=[{"name": "data2", "size": "1Gi", "mount-path": "/data2"}],
            remove=[],
            component_name="backend",
        )

        assert telling["added"] == 1
