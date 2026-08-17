"""The dashboard banner does not count a switched-off project as gezond (RC-31 step 3).

"Alle N projecten zijn gezond" is the sentence someone reads to know whether everything is
going well. A deployment that runs nothing on purpose has zero replicas, ArgoCD calls that
Healthy, and the sentence counted it in.

What stands there instead is a text choice, made in ``_dashboard_health_banner`` rather
than derived in the template -- which is exactly why it is tested here.
"""

from __future__ import annotations

from opi.services.catalog.sleep_mode.state import STATE_SLEEPING, SleepState, write
from opi.web.router import _dashboard_health_banner, _deployment_inactivity, _derive_project_health


def _counts(**kwargs: int) -> dict[str, int]:
    base = {"Healthy": 0, "Progressing": 0, "Degraded": 0, "Disabled": 0, "Inactive": 0, "Unknown": 0}
    base.update(kwargs)
    return base


def _project(*, disabled: bool = False, sleeping: bool = False) -> dict:
    project_data: dict = {
        "name": "productie",
        "components": [{"name": "frontend"}],
        "deployments": [
            {
                "name": "productie",
                "cluster": "odcn-production",
                "components": [{"reference": "frontend", "image": "ghcr.io/x/y:1", "disabled": disabled}],
            }
        ],
    }
    if sleeping:
        write(project_data, "productie", SleepState(state=STATE_SLEEPING))
    return project_data


class TestWhyADeploymentRunsNothing:
    def test_a_switched_off_deployment_is_reported_as_disabled(self) -> None:
        assert _deployment_inactivity(_project(disabled=True), "productie") == "Disabled"

    def test_a_sleeping_deployment_is_reported_separately(self) -> None:
        """Sleeping resolves itself on the first visit, switched off does not. One label
        for both would leave a reader unable to tell whether to act."""
        assert _deployment_inactivity(_project(sleeping=True), "productie") == "Inactive"

    def test_an_ordinary_deployment_reports_nothing(self) -> None:
        assert _deployment_inactivity(_project(), "productie") is None


class TestProjectHealthAggregation:
    def test_a_switched_off_deployment_keeps_a_project_out_of_healthy(self) -> None:
        assert _derive_project_health(["Disabled"]) == "Disabled"

    def test_it_does_not_outrank_a_real_problem(self) -> None:
        assert _derive_project_health(["Disabled", "Degraded"]) == "Degraded"
        assert _derive_project_health(["Inactive", "Progressing"]) == "Progressing"

    def test_it_does_outrank_healthy(self) -> None:
        """Half a project running is not "gezond" for banner purposes."""
        assert _derive_project_health(["Healthy", "Disabled"]) == "Disabled"

    def test_nothing_switched_off_behaves_exactly_as_before(self) -> None:
        assert _derive_project_health(["Healthy", "Healthy"]) == "Healthy"
        assert _derive_project_health([]) == "Unknown"


class TestTheBannerSentence:
    def test_the_old_sentence_stands_when_nothing_is_switched_off(self) -> None:
        banner = _dashboard_health_banner(_counts(Healthy=3))

        assert banner == {"kind": "success", "heading": "Alle 3 projecten zijn gezond", "lines": []}

    def test_one_healthy_project_keeps_its_own_wording(self) -> None:
        assert _dashboard_health_banner(_counts(Healthy=1))["heading"] == "Het project is gezond"

    def test_the_word_alle_disappears_as_soon_as_something_is_switched_off(self) -> None:
        banner = _dashboard_health_banner(_counts(Healthy=2, Disabled=1))

        assert banner["heading"] == "2 van de 3 projecten zijn gezond"
        assert banner["lines"] == ["1 project heeft een uitgeschakelde deployment"]
        assert banner["kind"] == "info"

    def test_switched_off_and_parked_are_named_apart(self) -> None:
        banner = _dashboard_health_banner(_counts(Healthy=1, Disabled=2, Inactive=1))

        # Slapend telt MEE als gezond: zo'n deployment doet precies wat er van hem
        # gevraagd is en komt vanzelf terug. Hij wordt wel apart benoemd, want je wilt
        # weten dat het zo is. Uitgeschakeld telt niet mee: dat blijft uit tot iemand het
        # aanzet. Eerder stond hier 1 van de 4, en dan las een slapend project als een
        # probleem.
        assert banner["heading"] == "2 van de 4 projecten zijn gezond"
        assert banner["lines"] == [
            "2 projecten hebben een uitgeschakelde deployment",
            "1 project heeft een slapende deployment",
        ]

    def test_no_healthy_projects_at_all_is_stated_plainly(self) -> None:
        banner = _dashboard_health_banner(_counts(Disabled=2))

        assert banner["heading"] == "Geen van je 2 projecten is gezond"

    def test_onbekend_wordt_benoemd_en_niet_verzwegen(self) -> None:
        """Alleen onbekende projecten leveren nu WEL een banner op.

        Hier stond dat dit None hoort te geven, en dat stilzwijgen was precies het
        probleem: bij twee gezonde en een leeg project sloeg de tak "geen uitgeschakelde
        en geen slapende" aan en stond er "Alle 2 projecten zijn gezond" terwijl het er
        drie waren. Een project zonder draaiende deployment is geen reden om te zwijgen,
        het is iets om te noemen.
        """
        banner = _dashboard_health_banner(_counts(Unknown=2))

        assert banner is not None
        assert banner["lines"] == ["2 projecten hebben nog geen deployment die iets draait"]

    def test_alle_geldt_alleen_als_het_er_alle_zijn(self) -> None:
        """Twee gezond naast een onbekend project: geen "alle", en het totaal klopt."""
        banner = _dashboard_health_banner(_counts(Healthy=2, Unknown=1))

        assert banner is not None
        assert banner["heading"] == "2 van de 3 projecten zijn gezond"
        assert banner["lines"] == ["1 project heeft nog geen deployment die iets draait"]
