"""De gegenereerde matrix tegen de tabel in ontwerp paragraaf 4, cel voor cel.

De tabel hieronder is overgeschreven uit ``plans/rechten-op-paden-tad-en-wies.md``,
paragraaf 4. Dat document is het contract; deze test is de plek waar de code eraan wordt
afgemeten. Wijkt de code af, dan is de code fout, tenzij iemand eerst het ontwerp wijzigt.

``v`` mag, ``v s`` mag met een verse step-up, leeg mag niet. De kolomvolgorde is die van
het ontwerp: viewer, developer, ops, admin, developer-ci, ops-ci.

Deze test vervangt ``tests/test_authz_pariteit.py``, die de rechten van VOOR deze taak
vastlegde. Die mocht weg zodra de nieuwe rechten gelden, want de twee spreken elkaar per
definitie tegen: de taak IS de gedragswijziging.
"""

from __future__ import annotations

import pytest
from opi.core.authz.actions import Action
from opi.core.authz.engine import Audience, all_rules
from opi.core.authz.matrix import COLUMNS, rows

#: (action, scope-naam) -> de zes cellen uit ontwerp paragraaf 4.
#: De scope-naam is "" voor een regel die er maar een rij in de matrix heeft.
ONTWERP: dict[tuple[str, str], tuple[str, ...]] = {
    # rij 1: project, deployments, logs, taken, back-ups lezen
    **{
        (naam, ""): ("v", "v", "v", "v", "", "")
        for naam in (
            "project.read",
            "project.list",
            "deployment.read",
            "component.read",
            "service.read",
            "database.schema-read",
            "backup.read",
            "restore.read",
            "logs.read",
            "subdomain.read",
            "cluster.read",
            "task.read",
            "sleep.read",
            "token.read",
        )
    },
    # rij 2 en 3: deployment aanmaken of wijzigen
    **{
        (naam, scope): cellen
        for naam in ("deployment.write", "deployment.refresh")
        for scope, cellen in (
            ("non-production", ("", "v", "v", "", "v", "")),
            ("production", ("", "", "v", "", "", "")),
        )
    },
    # rij 4 en 5: deployment verwijderen
    ("deployment.delete", "non-production"): ("", "v", "v", "", "v", ""),
    ("deployment.delete", "production"): ("", "", "v s", "", "", ""),
    # rij 6 en 7: image vervangen
    **{
        (naam, scope): cellen
        for naam in ("deployment.image-replace", "image.push")
        for scope, cellen in (
            ("non-production", ("", "v", "v", "", "v", "v")),
            ("production", ("", "", "v", "", "", "v")),
        )
    },
    # Rij 8 en 9: env-vars. Het ontwerp zet deze rij op niet-productie/productie; de code
    # zet hem op de COMPONENTscopes, want de env-vars hangen aan het component en de route
    # die ze bewerkt heeft geen deployment in het pad. De cellen zijn die van het ontwerp.
    ("env-vars.manage", "component-not-in-production"): ("", "v s", "v s", "", "", ""),
    ("env-vars.manage", "component-in-production"): ("", "", "v s", "", "", ""),
    # rij 10 en 11: service of component op projectniveau
    **{
        (naam, scope): cellen
        for naam in (
            "project.edit",
            "project.refresh",
            "component.add",
            "component.update",
            "component.link",
            "service.add",
            "service.configure",
            "service.write-values",
            "service.action",
            "registry.add",
            "attachment.manage",
        )
        for scope, cellen in (
            ("project-without-production", ("", "v", "v", "", "", "")),
            ("project-with-production", ("", "", "v", "", "", "")),
        )
    },
    # rij 12 en 13: component verwijderen
    ("component.delete", "component-not-in-production"): ("", "v", "v", "", "", ""),
    ("component.delete", "component-in-production"): ("", "", "v s", "", "", ""),
    # rij 14 en 15: job, databaseconsole, terugzetten en wat verder op EEN deployment werkt
    **{
        (naam, scope): cellen
        for naam in (
            "job.run",
            "database.console",
            "restore.run",
            "backup.create",
            "backup.delete",
            "database.clone",
            "database.schema-manage",
            "bucket.clone",
            "clone.validate",
            "resources.adjust",
            "resources.cleanup",
            "task.cancel",
            "sleep.wake",
        )
        for scope, cellen in (
            ("non-production", ("", "v", "v", "", "", "")),
            ("production", ("", "", "v", "", "", "")),
        )
    },
    # Niet in de tabel van het ontwerp, wel in het register (RC-392 securityreview). De
    # BRON van een kloon weegt als rij 2: klonen vanaf productie is ops, ongeacht het
    # doel. De hostnaam van een BESTAANDE deployment wijzigen is altijd ops, want de
    # domeingoedkeuring geldt per project en twee deployments kunnen dezelfde hostnaam
    # vormen.
    ("deployment.clone-from", "non-production"): ("", "v", "v", "", "v", ""),
    ("deployment.clone-from", "production"): ("", "", "v", "", "", ""),
    ("deployment.domain-set", ""): ("", "", "v", "", "", ""),
    # rij 16: de OTAP-markering
    ("environment.set", ""): ("", "", "v s", "", "", ""),
    # rij 17 tot en met 19: leden, tokens en het project zelf
    ("member.manage", ""): ("", "", "", "v s", "", ""),
    ("token.manage", ""): ("", "", "", "v s", "", ""),
    ("project.delete", ""): ("", "", "", "v s", "", ""),
    # Niet in de tabel van het ontwerp, wel in het register: de projectsleutel en de
    # audit. Allebei admin, zoals rij 17 en 18, en allebei zonder step-up omdat ze lezen.
    ("project-key.read", ""): ("", "", "", "v", "", ""),
    ("audit.read", ""): ("", "", "", "v", "", ""),
}


def _gegenereerd() -> dict[tuple[str, str], tuple[str, ...]]:
    scope_namen = {
        "": "",
        "productie": "production",
        "niet-productie": "non-production",
        "project met productie": "project-with-production",
        "project zonder productie": "project-without-production",
        "in productie": "component-in-production",
        "niet in productie": "component-not-in-production",
    }
    gevonden: dict[tuple[str, str], tuple[str, ...]] = {}
    for rij in rows():
        sleutel = (rij.action, scope_namen[rij.scope_label] if rij.scope_label else "")
        # The design writes v and v s; Cell.mark is screen-reader text, so read the flags.
        gevonden[sleutel] = tuple(("v s" if cel.step_up else "v") if cel.allowed else "" for cel in rij.cells)
    return gevonden


def test_de_kolommen_staan_in_de_volgorde_van_het_ontwerp() -> None:
    assert [rol.value for rol in COLUMNS] == ["viewer", "developer", "ops", "admin", "developer-ci", "ops-ci"]


def test_de_matrix_dekt_elke_projectregel_en_niets_meer() -> None:
    """De tabel hierboven en het register noemen precies dezelfde rijen."""
    assert set(_gegenereerd()) == set(ONTWERP)


@pytest.mark.parametrize("sleutel", sorted(ONTWERP))
def test_een_rij_uit_het_ontwerp(sleutel: tuple[str, str]) -> None:
    """Cel voor cel tegen de tabel uit ontwerp paragraaf 4."""
    assert _gegenereerd()[sleutel] == ONTWERP[sleutel], f"{sleutel[0]} ({sleutel[1] or 'overal'})"


def test_een_viewer_opent_alleen_leesacties() -> None:
    """Rij 1 van het ontwerp is de enige rij waar de viewerkolom gevuld is."""
    viewer = COLUMNS.index(next(rol for rol in COLUMNS if rol.value == "viewer"))
    per_action = {regel.action.value: regel.read for regel in all_rules()}
    for rij in rows():
        if rij.cells[viewer].allowed:
            assert per_action[rij.action], f"{rij.action} staat open voor een viewer maar is geen leesactie"


def test_elke_action_hoort_bij_precies_een_publiek() -> None:
    """Een regel is een projectrecht, of hij is het niet; niet allebei."""
    for regel in all_rules():
        if regel.audience is Audience.PROJECT:
            assert regel.grants or regel.action is Action.PROJECT_READ
        else:
            assert not regel.grants
