"""Of kind-config.yaml na envsubst nog geldige YAML is, in beide CNI-standen.

`task netpol:meet` rendert dit bestand met `envsubst` voor het naar `kind create
cluster` gaat; het aanmaken van dat cluster kan niet vanuit een sessie (de
docker-wachter blokkeert het). Deze test dekt wel het stuk ervoor: een verkeerd
gespelde variabele levert geen fout op bij envsubst, maar valt pas op als de
gerenderde tekst geen geldige YAML meer is of een veld een lege string wordt.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from ruamel.yaml import YAML

KIND_CONFIG = Path(__file__).parent / "kind-config.yaml"
ENVSUBST = shutil.which("envsubst")


def _render(*, disable_default_cni: str, kind_version: str = "1.32.0") -> dict:
    # Het absolute pad, want de env hieronder draagt geen PATH waarmee subprocess
    # een naam kan opzoeken.
    assert ENVSUBST
    result = subprocess.run(
        [ENVSUBST],
        input=KIND_CONFIG.read_text(),
        env={
            "NETPOL_DISABLE_DEFAULT_CNI": disable_default_cni,
            "NETPOL_KIND_VERSION": kind_version,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return YAML().load(result.stdout)


@pytest.mark.skipif(ENVSUBST is None, reason="envsubst is niet beschikbaar (gettext)")
class TestKindConfigRendert:
    def test_kindnet_zet_disabledefaultcni_op_false(self):
        document = _render(disable_default_cni="false")

        assert document["networking"]["disableDefaultCNI"] is False
        assert document["networking"]["ipFamily"] == "dual"

    def test_calico_zet_disabledefaultcni_op_true(self):
        document = _render(disable_default_cni="true")

        assert document["networking"]["disableDefaultCNI"] is True

    def test_de_node_image_draagt_de_gekozen_kind_versie(self):
        document = _render(disable_default_cni="false", kind_version="1.31.9")

        assert document["nodes"] == [{"role": "control-plane", "image": "kindest/node:v1.31.9"}]
