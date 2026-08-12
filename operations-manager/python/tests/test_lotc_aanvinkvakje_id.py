"""Een aanvinkvakje levert PRECIES EEN element met het veldpad als ``id``.

Waar dit vandaan komt: de generale repetitie
(``docs/generale-repetitie-2026-08-12.md``, bevinding 2) mat in de browser twee
elementen met dezelfde ``id`` voor een enkel vakje::

    DIV.lotc-checkbox-field   id=_services-config/keycloak/config/restrict-access/enabled
    NLDD-CHECKBOX-FIELD       id=_services-config/keycloak/config/restrict-access/enabled

De omhullende div kreeg de ``id`` van het component, en ons sjabloon zette hem
daarnaast in de attribuutbundel zodat hij op het besturingselement landde - het element
dat ``.checked`` draagt. Onder RVO landden beide op hetzelfde ``<input>``; onder NLDD
zijn het twee elementen. Dubbele ``id``'s zijn ongeldige HTML en breken ``label for=``
en ``aria-describedby``.

Opgelost in LOTC 762e570 (de component zet de ``id`` nu op het besturingselement) plus
onze kant: de ``id`` is uit de bundel gehaald. Deze test bewaakt beide helften tegelijk,
want hij meet de GERENDERDE HTML - een terugval aan welke kant dan ook laat hem vallen.

De browserkant hiervan staat in ``tests/e2e/test_aanvinkvakje.py``: die toetst dat
``[id='<pad>']`` het element oplevert dat ``.checked`` draagt.

Run: uv run pytest tests/test_lotc_aanvinkvakje_id.py -q
"""

from __future__ import annotations

import re

from opi.forms.field import FormField
from opi.forms.widgets.lotc import LOTCWidgetAdapter

#: Het pad uit de meting: een virtueel dienstconfigpad met schuine strepen erin.
PAD = "_services-config/keycloak/config/restrict-access/enabled"


def _veld(**kwargs: object) -> FormField:
    defaults: dict[str, object] = {
        "name": "enabled",
        "path": PAD,
        "schema_type": bool,
        "widget_type": "checkbox",
        "label": "Toegang beperken",
    }
    defaults.update(kwargs)
    return FormField(**defaults)  # type: ignore[arg-type]


def _ids(html: str) -> list[str]:
    return re.findall(r'\bid="([^"]*)"', html)


def test_enkel_vakje_heeft_het_pad_maar_een_keer_als_id() -> None:
    """De poort: een document met twee gelijke id's is niet acceptabel."""
    html = LOTCWidgetAdapter().render_checkbox(_veld())
    assert _ids(html).count(PAD) == 1, f"verwacht precies een id={PAD}, kreeg: {_ids(html)}\n{html}"


def test_de_id_staat_op_het_besturingselement() -> None:
    """Niet op de omhulling: het besturingselement is wat ``.checked`` draagt.

    Zonder deze regel is de test hierboven ook groen als de id juist van de besturing
    verdwijnt, en dan is elk vakje onvindbaar in plaats van dubbel.
    """
    html = LOTCWidgetAdapter().render_checkbox(_veld())
    besturing = re.search(r"<nldd-checkbox-field\b[^>]*>", html)
    assert besturing is not None, f"geen <nldd-checkbox-field> gerenderd:\n{html}"
    assert f'id="{PAD}"' in besturing.group(0), f"de id staat niet op de besturing: {besturing.group(0)}"
    omhulling = re.search(r'<div class="lotc-checkbox-field"[^>]*>', html)
    assert omhulling is not None, f"geen omhullende div gerenderd:\n{html}"
    assert "id=" not in omhulling.group(0), f"de omhulling draagt nog een id: {omhulling.group(0)}"


def test_hulptekst_en_foutmelding_houden_hun_afgeleide_ids() -> None:
    """De prijs die het NIET mocht kosten: ``-help`` en ``-error`` blijven bestaan."""
    html = LOTCWidgetAdapter().render_checkbox(_veld(help_text="Alleen voor leden", errors=["Verplicht"]))
    ids = _ids(html)
    assert f"{PAD}-help" in ids, f"de hulptekst mist zijn id: {ids}"
    assert f"{PAD}-error" in ids, f"de foutmelding mist zijn id: {ids}"
    assert len(ids) == len(set(ids)), f"er staan dubbele id's in: {ids}"


def test_groep_levert_ook_geen_dubbele_ids() -> None:
    """Dezelfde componentvorm, dezelfde poort - een groep hoort het ook te halen."""
    veld = _veld(
        widget_type="checkbox_group",
        options=[{"value": "keycloak", "label": "Keycloak"}, {"value": "minio", "label": "MinIO"}],
        value=["keycloak"],
    )
    html = LOTCWidgetAdapter().render_checkbox_group(veld)
    ids = _ids(html)
    assert len(ids) == len(set(ids)), f"er staan dubbele id's in: {ids}"
