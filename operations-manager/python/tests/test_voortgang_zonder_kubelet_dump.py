"""De voortgangslijst is een LIJST, geen dumpplek.

Aanleiding: een refresh van een project met vijftien deployments leverde één melding van
ruim dertienduizend tekens op. De oorzaak zat niet in wat er misging maar in waar het werd
neergezet: de rauwe kubelet-tekst (762 tekens per component, met dezelfde fout twee keer
erin) kwam integraal terecht in de TITEL van een regel in de voortgangslijst, en daarna
nog een keer in een afsluitende melding die alle waarschuwingen aan elkaar plakte.

Deze tests borgen de drie plekken waar dat nu wordt tegengehouden: de reden die de
pod-inspectie teruggeeft, de harde grens op een stapnaam, en de groepering van de
componentfouten voor het paneel eronder.
"""

from opi.core.task_manager import MAX_STEP_NAME, clamp_step_text
from opi.services.event_interpreter import group_component_failures
from opi.services.oom_watcher import _describe_pod_waiting

# De echte melding zoals CRI-O hem op productie voor wies produceerde: de fout staat er
# twee keer in, een keer als "pull image err" en een keer als "artifact err".
KUBELET_IMAGE_PULL = (
    'Back-off pulling image "rcr.rijksapps.nl/ghcr-rig/rijksictgilde/wies:pr-274": '
    "ErrImagePull: unable to pull image or OCI artifact: pull image err: initializing source "
    "docker://rcr.rijksapps.nl/ghcr-rig/rijksictgilde/wies:pr-274: reading manifest pr-274 in "
    "rcr.rijksapps.nl/ghcr-rig/rijksictgilde/wies: manifest unknown: the requested image may not "
    "exist in the upstream registry, or the configured Quay organization credentials have "
    "insufficient rights to access it (404); artifact err: get manifest: build image source: "
    "reading manifest pr-274 in rcr.rijksapps.nl/ghcr-rig/rijksictgilde/wies: manifest unknown: "
    "the requested image may not exist in the upstream registry, or the configured Quay "
    "organization credentials have insufficient rights to access it (404)"
)


def _waiting_pod(reason: str, message: str) -> dict:
    return {
        "status": {
            "phase": "Pending",
            "containerStatuses": [
                {"name": "app", "ready": False, "state": {"waiting": {"reason": reason, "message": message}}}
            ],
        }
    }


class TestDeRedenPastOpEenRegel:
    def test_image_pull_meldt_de_reden_zonder_de_registry_dump(self) -> None:
        """De 762 tekens registry-tekst hoort niet in een regeltitel. Welk image het is en
        wat eraan te doen valt staat in component_failures, met een vertaalde titel."""
        reden = _describe_pod_waiting(_waiting_pod("ImagePullBackOff", KUBELET_IMAGE_PULL))

        assert reden == "image ophalen mislukt (ImagePullBackOff)"
        assert "artifact err" not in reden
        assert "manifest unknown" not in reden

    def test_andere_redenen_houden_hun_bericht_maar_afgekapt(self) -> None:
        """Een planningsfout of een crash meldt wel iets bruikbaars in zijn message, dus die
        blijft staan. Alleen de lengte is begrensd, zodat geen enkele bron kan ontsporen."""
        reden = _describe_pod_waiting(_waiting_pod("CrashLoopBackOff", "x" * 900))

        assert reden.startswith("blijft herstarten na een crash: ")
        assert len(reden) < 200

    def test_een_kort_bericht_blijft_ongemoeid(self) -> None:
        reden = _describe_pod_waiting(_waiting_pod("CreateContainerConfigError", "secret 'db' not found"))

        assert reden == "CreateContainerConfigError: secret 'db' not found"


class TestDeStapnaamIsEenLabel:
    def test_een_dump_wordt_afgekapt(self) -> None:
        geknipt = clamp_step_text(KUBELET_IMAGE_PULL)

        assert len(geknipt) == MAX_STEP_NAME
        assert geknipt.endswith("…")

    def test_een_gewone_melding_blijft_heel(self) -> None:
        """De grens is een vangnet, geen opmaakmiddel: de langste bestaande melding, de
        ArgoCD-geruststelling bij een nieuw project, moet er ongeschonden doorheen."""
        melding = (
            "Duurt het wachten op ArgoCD lang, dan betekent een time-out-melding niet dat het "
            "aanmaken is mislukt: alleen dat de wachttijd is verstreken. Het project wordt dan "
            "vrijwel zeker gewoon aangemaakt."
        )

        assert clamp_step_text(melding) == melding


class TestComponentfoutenWordenGegroepeerd:
    def _fout(self, deployment: str, component: str, titel: str, ernst: str = "actionable") -> dict:
        return {
            "component": component,
            "deployment": deployment,
            "failure_type": "image_pull",
            "message": KUBELET_IMAGE_PULL,
            "title": titel,
            "suggestion": "Controleer of de image publiek toegankelijk is.",
            "severity": ernst,
        }

    def test_hetzelfde_probleem_wordt_een_blok(self) -> None:
        """Zestien componenten die hun image niet kunnen ophalen zijn voor de lezer een
        ding, niet zestien. Zonder groepering stond dezelfde suggestie zestien keer op het
        scherm en was juist het verband niet te zien."""
        titel = "Container image kan niet worden opgehaald"
        groepen = group_component_failures(
            [
                self._fout("pr-274", "frontend", titel),
                self._fout("pr-274", "worker", titel),
                self._fout("pr-379", "frontend", titel),
            ]
        )

        assert len(groepen) == 1
        groep = groepen[0]
        assert groep["title"] == titel
        assert groep["component_count"] == 3
        assert groep["deployments"] == [
            {"name": "pr-274", "components": ["frontend", "worker"]},
            {"name": "pr-379", "components": ["frontend"]},
        ]

    def test_verschillende_problemen_blijven_gescheiden(self) -> None:
        groepen = group_component_failures(
            [
                self._fout("pr-274", "frontend", "Container image kan niet worden opgehaald"),
                self._fout("pr-460", "worker", "Applicatie crasht herhaaldelijk"),
            ]
        )

        assert [g["title"] for g in groepen] == [
            "Container image kan niet worden opgehaald",
            "Applicatie crasht herhaaldelijk",
        ]

    def test_een_groep_is_zo_ernstig_als_zijn_ernstigste_lid(self) -> None:
        """Een registry die niet antwoordt is informational en hoort niet rood te zijn. Zit
        er een component bij waar de gebruiker wel iets aan moet doen, dan telt die."""
        titel = "Container image kan niet worden opgehaald"
        groepen = group_component_failures(
            [
                self._fout("pr-379", "worker", titel, ernst="informational"),
                self._fout("pr-379", "frontend", titel, ernst="actionable"),
            ]
        )

        assert groepen[0]["severity"] == "actionable"

    def test_alleen_informational_blijft_informational(self) -> None:
        groepen = group_component_failures(
            [self._fout("nldd-test", "worker", "Registry kon de container image niet leveren", ernst="informational")]
        )

        assert groepen[0]["severity"] == "informational"

    def test_geen_fouten_geeft_geen_groepen(self) -> None:
        assert group_component_failures(None) == []
        assert group_component_failures([]) == []


# ---------------------------------------------------------------------------
# Het paneel onder de stappenlijst
# ---------------------------------------------------------------------------


class TestHetPaneelStaatErOokNaEenGeslaagdeVerwerking:
    """ "Uitgerold, maar niet gezond" is formeel een succes.

    Het paneel met componentfouten werd alleen bij ``failed`` ingeladen, dus juist in deze
    uitkomst was er geen gestructureerd kanaal en werd het hele verhaal als proza in de
    naam van een voortgangsstap geduwd. Dat is de regel van dertienduizend tekens.
    """

    def _render(self, status: str) -> str:
        from opi.core.templates_lotc import templates_lotc

        return templates_lotc.get_template("partials/task_progress_fragment.html.j2").render(
            {
                "task_id": "task-under-test",
                "progress_url": "/projects/wies/task-progress/task-under-test",
                "progress": 100,
                "current_step": "Klaar",
                "status": status,
                "tasks": [],
                "project_name": "wies",
                "component_failures": [
                    {
                        "component": "frontend",
                        "deployment": "pr-274",
                        "failure_type": "image_pull",
                        "message": KUBELET_IMAGE_PULL,
                        "title": "Container image kan niet worden opgehaald",
                        "suggestion": "Controleer of de image publiek toegankelijk is.",
                        "severity": "actionable",
                    },
                    {
                        "component": "worker",
                        "deployment": "pr-274",
                        "failure_type": "image_pull",
                        "message": KUBELET_IMAGE_PULL,
                        "title": "Container image kan niet worden opgehaald",
                        "suggestion": "Controleer of de image publiek toegankelijk is.",
                        "severity": "actionable",
                    },
                ],
            }
        )

    def test_de_groep_verschijnt_bij_completed(self) -> None:
        rendered = self._render("completed")

        assert "Container image kan niet worden opgehaald" in rendered
        assert "pr-274" in rendered
        assert "frontend, worker" in rendered

    def test_de_suggestie_staat_er_een_keer_en_niet_per_component(self) -> None:
        rendered = self._render("completed")

        assert rendered.count("Controleer of de image publiek toegankelijk is.") == 1

    def test_de_rauwe_kubelet_tekst_komt_niet_op_het_scherm(self) -> None:
        rendered = self._render("completed")

        assert "artifact err" not in rendered
        assert "manifest unknown" not in rendered


def test_een_suggestie_die_over_een_van_meer_images_gaat_heet_een_voorbeeld() -> None:
    """De suggestie noemt een concreet image. Wordt er één getoond voor twaalf componenten
    die elk hun eigen image hebben, dan doet die anders alsof hij over alles gaat."""
    basis = {"failure_type": "image_pull", "message": "", "title": "Container image kan niet worden opgehaald"}
    groepen = group_component_failures(
        [
            {**basis, "component": "frontend", "deployment": "pr-274", "suggestion": "Haal wies:pr-274 op."},
            {**basis, "component": "frontend", "deployment": "pr-379", "suggestion": "Haal wies:pr-379 op."},
        ]
    )

    assert groepen[0]["suggestion_is_example"] is True


def test_een_gedeelde_suggestie_is_geen_voorbeeld() -> None:
    basis = {"failure_type": "crash_loop", "message": "", "title": "Applicatie crasht herhaaldelijk"}
    groepen = group_component_failures(
        [
            {**basis, "component": "worker", "deployment": "pr-460", "suggestion": "Bekijk de logs."},
            {**basis, "component": "worker", "deployment": "pr-491", "suggestion": "Bekijk de logs."},
        ]
    )

    assert groepen[0]["suggestion_is_example"] is False


class TestDeLogboeklinkVolgtDePagina:
    """De voortgangspagina laadt het logboekpaneel niet, dus daar zou de link niets doen.

    Dat onderscheid loopt via ``{% with show_log_links = false %}`` rond een ``include``,
    en dat is precies het soort scoping dat stil kapot gaat. Vandaar een poort.
    """

    CONTEXT = {
        "task_id": "t",
        "progress_url": "/x",
        "progress": 100,
        "current_step": "Klaar",
        "status": "completed",
        "tasks": [],
        "project_name": "wies",
        "component_failures": [
            {
                "component": "worker",
                "deployment": "pr-460",
                "failure_type": "crash_loop",
                "message": "back-off 5m0s restarting failed container=app",
                "title": "Applicatie crasht herhaaldelijk",
                "suggestion": "Bekijk de logs.",
                "severity": "actionable",
            }
        ],
    }

    def _render(self, sjabloon: str) -> str:
        from opi.core.templates_lotc import templates_lotc

        return templates_lotc.get_template(sjabloon).render(self.CONTEXT)

    def test_de_voortgangspagina_toont_het_paneel_zonder_dode_link(self) -> None:
        rendered = self._render("bg/_task-progress.html.j2")

        assert "Applicatie crasht herhaaldelijk" in rendered
        assert "openLogViewer" not in rendered

    def test_het_gedeelde_fragment_houdt_de_link_wel(self) -> None:
        rendered = self._render("partials/task_progress_fragment.html.j2")

        assert "Applicatie crasht herhaaldelijk" in rendered
        assert "openLogViewer" in rendered
