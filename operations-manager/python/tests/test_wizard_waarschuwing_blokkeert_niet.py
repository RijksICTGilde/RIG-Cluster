"""Een waarschuwing houdt een modal-wizardstap niet tegen; een fout wel.

Gemeld tijdens de doorloop van RC-118: je zet een eigen domein op een deployment, klikt
Volgende, en je blijft op dezelfde stap staan. Geen foutmelding, geen uitleg -- alleen een
tekst die zegt dat dit cluster geen certificaat voor een eigen domein kan aanvragen.

De server zei het zelf in de log:

    Wizard step .../domain-edit-0 did not advance:
      field_errors={} global_errors=[] warnings={'...base-domain': ["Het cluster ..."]}

De poort stond op ``errors or section_global_errors or section_warnings``. Daarmee blokkeert
een WAARSCHUWING de navigatie, terwijl een waarschuwing per definitie informeert en een fout
blokkeert. Erger nog: een waarschuwing rendert als gewone tekst en niet als foutmelding, dus
er stond niets op het scherm dat verklaarde waarom je niet verder kwam.

Wat het extra zuur maakte: de stap waar je naartoe wilde IS de plek waar je het oplost.
``build_domain_cert_section`` ("Certificaten per component") zet ``tls`` en ``attachment``
per component. Die stap was onbereikbaar, dus de conclusie op het scherm was "die
mogelijkheid bestaat hier niet".

Deze tests pinnen de drie standen vast: fout blokkeert, waarschuwing niet, en de
waarschuwing verdwijnt niet zodra je doorklikt.
"""

from __future__ import annotations


class TestDeDomeinwizardHeeftEenCertificaatstap:
    """De stap waar de gebruiker naartoe moet, bestaat en staat achter de domeinstap."""

    def test_de_flow_heeft_twee_stappen_in_deze_volgorde(self) -> None:
        from opi.forms.visualizers.flows import build_domain_edit_flow

        flow = build_domain_edit_flow(0)
        assert [s.section_id for s in flow.sections] == ["domain-edit-0", "domain-cert-0"]

    def test_beide_stappen_zijn_altijd_actief(self) -> None:
        """Geen ``show_when``, dus de certificaatstap kan niet wegvallen."""
        from opi.forms.visualizers.flows import build_domain_edit_flow
        from opi.forms.wizard.resolver import resolve_active_section_ids

        flow = build_domain_edit_flow(0)
        assert resolve_active_section_ids(flow, {}) == ["domain-edit-0", "domain-cert-0"]

    def test_de_certificaatstap_regelt_tls_per_component(self) -> None:
        """TLS zit op het COMPONENT, en die stap is precies daarvoor."""
        from opi.forms.visualizers.wizard_sections import build_domain_cert_section

        section = build_domain_cert_section(0)
        assert section.title == "Certificaten per component"
        velden = str(section.layout)
        assert "services/publish-on-web/config/tls" in velden
        assert "services/publish-on-web/config/attachment" in velden


class TestAlleenFoutenHoudenDeStapTegen:
    """De poort zelf, op de bron gelezen: warnings staan er niet meer in."""

    def _blokkeerregel(self) -> str:
        from pathlib import Path

        bron = Path(__file__).resolve().parents[1] / "opi" / "web" / "router_detail_edit.py"
        for regel in bron.read_text().splitlines():
            gestript = regel.strip()
            if gestript.startswith("if errors or section_global_errors"):
                return gestript
        raise AssertionError("de blokkeerpoort van de modal-wizardstap is niet gevonden")

    def test_een_waarschuwing_blokkeert_de_navigatie_niet(self) -> None:
        assert "section_warnings" not in self._blokkeerregel()

    def test_een_fout_blokkeert_wel(self) -> None:
        regel = self._blokkeerregel()
        assert "errors" in regel
        assert "section_global_errors" in regel

    def test_de_waarschuwing_reist_mee_naar_de_volgende_stap(self) -> None:
        """Niet blokkeren mag niet betekenen: stilzwijgend weggooien."""
        from pathlib import Path

        bron = Path(__file__).resolve().parents[1] / "opi" / "web" / "router_detail_edit.py"
        tekst = bron.read_text()
        na_volgende_stap = tekst.split("# More steps to go", 1)[1]
        assert "warnings=section_warnings or None" in na_volgende_stap
