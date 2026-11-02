"""Wat DIT verzoek onderweg achterliet, voor de auditrij aan het eind.

De hook rond de rechtenbeslissing (``opi/middleware/audit.py``) schrijft de rij na het
antwoord, en kent op dat moment drie dingen niet die alleen tijdens het verzoek bekend
zijn: welke commits van het projectbestand het maakte, welke taak het startte, en wat de
route zelf aan velden noemde. Ze staan hier als contextvar per verzoek, zoals de actor in
``opi/core/actor.py``, zodat de schrijvers (de projectstore, de taakdienst, de poort) de
hook niet hoeven te kennen.

Het spoor wordt door de hook GEOPEND (``begin_trail``) en bestaat alleen binnen een
verzoek. Buiten een verzoek legt een schrijver niets vast: een commit uit de nachtelijke
``ResourceTuningScheduler`` draait in een enkele langlevende taak, en zonder die grens
zou de contextvar daar eindeloos doorgroeien zonder dat iemand hem ooit leest.

Elk verzoek draait in zijn eigen kopie van de context, dus een ander verzoek ziet deze
waarden niet.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from opi.core.auth_decorators import velden_uit_formulier
from opi.core.authz.actions import audit_fields_of

if TYPE_CHECKING:
    import uuid

    from starlette.requests import Request

#: De enige body die voor de audit wordt gelezen. Een ``multipart``-body blijft erbuiten:
#: dat is de vorm van een image-upload, en die in zijn geheel ontleden om er een veldnaam
#: uit te halen zou de upload door het geheugen halen voor een logregel.
FORMULIER_TYPE = "application/x-www-form-urlencoded"

#: De lengte waarop een veldwaarde wordt afgekapt, zoals ``record_action`` dat voor
#: ``origin``, ``actor`` en ``summary`` doet. ``data`` is JSONB en heeft geen
#: kolombreedte, dus zonder deze grens zet een aanroeper er zoveel in als zijn body
#: groot is, en dat blijft een jaar staan. Een image met digest past er ruim in.
MAX_VELDWAARDE = 256


@dataclass(slots=True)
class Trail:
    """Het spoor van een verzoek, in de vorm waarin de rij het draagt."""

    commit_shas: list[str] = field(default_factory=list)
    task_id: uuid.UUID | None = None
    #: ``object`` en niet ``str``, want dat is de vorm die ``record_action`` leest.
    fields: dict[str, object] = field(default_factory=dict)


_trail: contextvars.ContextVar[Trail | None] = contextvars.ContextVar("audit_trail", default=None)


def begin_trail() -> None:
    """Open het spoor van dit verzoek; alleen de hook doet dit."""
    _trail.set(Trail())


def record_commit(ref: str) -> None:
    """Leg een commit van een projectbestand vast; een verzoek kan er meer dan een maken."""
    spoor = _trail.get()
    if spoor is not None:
        spoor.commit_shas.append(ref)


def record_task(task_id: uuid.UUID) -> None:
    """Leg de taak vast die dit verzoek startte. De eerste blijft staan.

    De rij heeft er een kolom voor en geen lijst: hij noemt de taak waarin het werk van
    dit verzoek verderliep, en dat is de eerste die eruit kwam.
    """
    spoor = _trail.get()
    if spoor is not None and spoor.task_id is None:
        spoor.task_id = task_id


async def capture_fields(request: Request, endpoint: Any) -> None:
    """Leg de velden vast die DEZE route zelf noemt, met hun ingediende waarde.

    Alleen de genoemde velden, nooit een hele body, en elk afgekapt op
    :data:`MAX_VELDWAARDE`: wat hier in komt blijft een jaar staan, ook nadat de
    bewaarlus ``actor`` en ``origin`` heeft leeggemaakt, dus een route noemt nooit een
    veld dat een persoon kan aanwijzen (``features/audit.md``).

    Bij de poort en niet in de hook: de hook draait na het antwoord, en het formulier van
    dit verzoek is dan niet meer te lezen. Wel pas NADAT de poort de aanroeper heeft
    toegelaten, want een aanroeper die niets mag hoort geen body te laten ontleden.
    """
    spoor = _trail.get()
    namen = audit_fields_of(endpoint)
    if spoor is None or not namen:
        return
    gevonden = {
        naam: str(waarde)[:MAX_VELDWAARDE]
        for naam, waarde in (*request.path_params.items(), *request.query_params.items())
        if naam in namen
    }
    ontbreekt = tuple(naam for naam in namen if naam not in gevonden)
    if ontbreekt and request.headers.get("content-type", "").startswith(FORMULIER_TYPE):
        uit_formulier = await velden_uit_formulier(request, ontbreekt)
        gevonden.update({naam: waarde[:MAX_VELDWAARDE] for naam, waarde in uit_formulier.items()})
    spoor.fields = dict(gevonden)


def trail() -> Trail:
    """Het spoor van dit verzoek, of een leeg spoor buiten een verzoek."""
    return _trail.get() or Trail()
