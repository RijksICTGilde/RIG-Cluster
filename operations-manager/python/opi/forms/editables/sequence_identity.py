"""De identiteit van een rij in een reeks, zoals de inzending hem draagt.

De sleutel van een rij is ``reference`` of ``name``, en in een bewerkformulier is dat
veld readonly: de widget rendert het als ``disabled``, en een disabled veld stuurt de
browser niet mee. Zonder sleutel moest de verwerker de rijen op INDEX paren met de
opgeslagen lijst, en dan verschuift elke rij achter een verwijderde rij: het component
dat bleef kreeg de configuratie van zijn buur, en de deployment werd daar consequent op
bijgewerkt (RC-378).

Daarom stuurt elke gerenderde rij haar sleutel mee als verborgen veld, net zoals een
gerenderde reeks haar pad meestuurt (``rendered_sequences.py``), en paart de verwerker
daarop. Een verborgen sleutel is geen gegeven dat de gebruiker mag wijzigen: de
verwerker zet het readonly veld na de merge terug uit de gepaarde rij, dus de sleutel
bepaalt alleen MET WELKE opgeslagen rij deze rij hoort.
"""

from __future__ import annotations

from typing import Any

#: De velden waaraan een rij te herkennen is, in volgorde van voorkeur.
IDENTITY_KEYS = ("reference", "name")


def identity_of(item: Any) -> tuple[str, Any] | None:
    """Het sleutelveld en de sleutelwaarde van *item*, of None als het er geen heeft."""
    if not isinstance(item, dict):
        return None
    for key in IDENTITY_KEYS:
        value = item.get(key)
        if value:
            return key, value
    return None


def match_by_identity(item: Any, originals: list[Any], index: int) -> Any:
    """De rij uit *originals* die bij *item* hoort: op sleutel, anders op *index*."""
    identity = identity_of(item)
    if identity is not None:
        key, value = identity
        for original in originals:
            if isinstance(original, dict) and original.get(key) == value:
                return original
    if 0 <= index < len(originals):
        return originals[index]
    return None
