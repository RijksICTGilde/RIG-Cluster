"""Er staat geen ``Queued`` meer op het scherm, en bestaande taken blijven leesbaar.

``Queued`` is niet zomaar een label: het is de KOLOMSTANDAARD van
``async_tasks.current_step``, dus hij staat in de database en in elke rij die er al is.
Blind de opgeslagen waarde vertalen zou drie dingen tegelijk raken - de standaard, elke
schrijver, en de bestaande rijen - en een migratie die halverwege stopt laat een tabel met
twee talen achter.

Gekozen is daarom voor vertalen bij de WEERGAVE. Deze test legt die keuze vast aan beide
kanten: de weergave is Nederlands, en de opgeslagen waarde verandert niet.

Dezelfde behandeling krijgen de drie andere Engelse stapregels die de
voortgangsschrijvers wegschrijven (``Starting...``, ``Done``, ``Failed: <fout>``). Die
stonden niet in de opdracht maar staan op precies hetzelfde scherm.
"""

from __future__ import annotations

from opi.core.async_task_schema import ASYNC_TASKS_TABLE_SQL
from opi.web.router import _v2_task_to_template_context
from opi.web.router_tasks import _normalize_task
from opi.web.stap_labels import stap_label


class TestDeVertaling:
    def test_queued_is_nederlands(self) -> None:
        assert stap_label("Queued") == "In wachtrij"

    def test_de_andere_vaste_regels_ook(self) -> None:
        assert stap_label("Starting...") == "Wordt gestart..."
        assert stap_label("Done") == "Klaar"

    def test_een_mislukking_houdt_zijn_fouttekst(self) -> None:
        """Alleen de kop vertaalt; wat erachter staat is de melding zelf."""
        assert stap_label("Failed: connection refused") == "Mislukt: connection refused"

    def test_een_nederlandse_stap_gaat_ongewijzigd_door(self) -> None:
        """De meeste stapregels worden al in het Nederlands geschreven."""
        assert stap_label("Realm aanmaken - productie") == "Realm aanmaken - productie"

    def test_leeg_blijft_leeg(self) -> None:
        assert stap_label(None) is None
        assert stap_label("") == ""


class TestDeTweeSchermen:
    """De twee plekken waar een stapregel een mens bereikt."""

    def test_de_takentabel_toont_de_vertaling(self) -> None:
        rij = _normalize_task({"task_type": "refresh_project", "status": "pending", "current_step": "Queued"})

        assert rij["step"] == "In wachtrij"

    def test_het_voortgangsblok_toont_de_vertaling(self) -> None:
        context = _v2_task_to_template_context(
            {"status": "pending", "current_step": "Queued", "progress_percent": 0}, "een-project"
        )

        assert context["current_step"] == "In wachtrij"

    def test_een_taak_zonder_stap_houdt_zijn_eigen_zin(self) -> None:
        """Bewaak de bewaker: de vertaling mag de bestaande terugval niet opeten."""
        context = _v2_task_to_template_context({"status": "pending", "progress_percent": 0}, "een-project")

        assert context["current_step"] == "Verwerking gestart..."


def test_de_opgeslagen_waarde_is_niet_meevertaald() -> None:
    """De keuze staat aan deze kant vast: de kolomstandaard blijft zoals hij was.

    Zou iemand hem alsnog omzetten, dan hoort daar het hele pakket bij (lezers en
    bestaande rijen) en niet alleen deze regel - en dan hoort deze test mee te veranderen.
    """
    assert "DEFAULT 'Queued'" in ASYNC_TASKS_TABLE_SQL
