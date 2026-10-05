"""Only registry auth/permission image-pull failures are real errors.

A not-found / not-yet-built image is expected churn (CI/CD, PR builds) and must be
a WARNING, not an ERROR that fails the deploy task and pages ops. This locks the
classifier that routes auth failures to sync_failures (ERROR) and everything else
to health_warnings (WARN).
"""

import ast
import pathlib

import opi
from opi.handlers.project_file_handler import image_is_confirmed_absent
from opi.manager.project_manager import ProjectManager, _is_image_pull_auth_error


def test_auth_failures_are_real_errors() -> None:
    # The quay-rig outage message we actually saw in prod.
    assert _is_image_pull_auth_error(
        "initializing source docker://rcr.rijksapps.nl/quay-rig/oauth2-proxy/oauth2-proxy:v7.7.1: "
        "unable to retrieve auth token: invalid username/password: authentication required"
    )
    assert _is_image_pull_auth_error("401 Unauthorized")
    assert _is_image_pull_auth_error("pull access denied, 403 Forbidden")
    assert _is_image_pull_auth_error("no basic auth credentials")


def test_not_found_and_transient_are_not_errors() -> None:
    assert not _is_image_pull_auth_error("manifest unknown")
    assert not _is_image_pull_auth_error("not found: manifest for repo:tag not found")
    # Bare "denied" is deliberately not auth: ghcr returns it for a private-or-missing
    # tag too, so a not-built PR image would otherwise misclassify as an error.
    assert not _is_image_pull_auth_error("denied: requested access to the resource is denied")
    assert not _is_image_pull_auth_error("ImagePullBackOff")
    assert not _is_image_pull_auth_error("")
    assert not _is_image_pull_auth_error(None)


class TestDeAuthEscalatieWordtAlleenGevraagdOverEenANTWOORD:
    """Een transportstoring mag geen uitrol laten falen op de SPELLING van een tag.

    De markers hierboven bevatten kale ``401`` en ``403``, en die kunnen in een image-tag
    staan: ``pr-403-abc1234``. Zolang de vraag alleen gesteld wordt over een melding waar
    de registry werkelijk antwoord gaf, kan dat niet botsen. Valt die poort weg, dan
    escaleert de mirror-storing van 2026-09-10 op zo'n tag naar ``sync_failures`` en faalt
    de uitrol: dat was voor RC-243 niet zo en mag er niet per ongeluk bijkomen.

    Dit is een grendel op de VORM en niet op het gedrag, omdat het beslispunt midden in
    ``process_project_from_git`` zit (een methode van honderden regels, binnen een
    ``except DeploymentHealthError``) en niet aan te roepen is zonder die te knippen. Dat
    knippen valt buiten RC-243; deze grendel is wat er wel te meten is.
    """

    def _lus(self) -> ast.For:
        """De lus over de image-pull-fouten, met de omliggende knopen."""
        bron = pathlib.Path(opi.__file__).parent / "manager" / "project_manager.py"
        boom = ast.parse(bron.read_text(encoding="utf-8"))
        methode = next(
            knoop
            for knoop in ast.walk(boom)
            if isinstance(knoop, ast.AsyncFunctionDef)
            and knoop.name == ProjectManager.process_project_from_git.__name__
        )
        lussen = [
            knoop
            for knoop in ast.walk(methode)
            if isinstance(knoop, ast.For) and ast.unparse(knoop.iter) == "image_pull_failures"
        ]
        assert len(lussen) == 1, f"verwacht een lus over image_pull_failures, gevonden: {len(lussen)}"
        return lussen[0]

    def _tak_zonder_antwoord(self, lus: ast.For) -> ast.If:
        vraag = f"not {image_is_confirmed_absent.__name__}("
        takken = [k for k in ast.walk(lus) if isinstance(k, ast.If) and ast.unparse(k.test).startswith(vraag)]
        assert len(takken) == 1, f"verwacht een tak op '{vraag}...', gevonden: {len(takken)}"
        return takken[0]

    def test_de_tak_zonder_antwoord_verlaat_de_ronde(self) -> None:
        tak = self._tak_zonder_antwoord(self._lus())

        assert isinstance(tak.body[-1], ast.Continue), (
            "de tak voor een melding zonder antwoord van de registry valt door naar de rest "
            "van de lus; dan krijgt een storing zowel de registry-zin als de "
            "ImagePullBackOff-waarschuwing, en op een tag met 401/403 erin ook een "
            "sync_failure die de uitrol laat falen"
        )

    def test_de_auth_vraag_staat_buiten_die_tak(self) -> None:
        lus = self._lus()
        tak = self._tak_zonder_antwoord(lus)
        naam = _is_image_pull_auth_error.__name__

        def vraagt_auth(knoop: ast.AST) -> bool:
            return any(isinstance(k, ast.Call) and ast.unparse(k.func) == naam for k in ast.walk(knoop))

        # De kanarie: de lus stelt de vraag werkelijk, anders bewijst de bewering erna niets.
        assert vraagt_auth(lus), f"de lus roept {naam} niet aan, dus deze grendel meet niets"
        assert not vraagt_auth(tak), f"{naam} wordt ook gevraagd over een melding waar de registry niet op antwoordde"
