"""De ``checksum/config``-annotatie moet een string blijven, ook als de hash uit cijfers bestaat.

De ArgoCD CMP-plugin stempelt de hash van alle Secrets en ConfigMaps op elk pod-template.
Die hash is de eerste zestien tekens van een sha256 in hex, en die gaan zo nu en dan door
voor een getal: allemaal cijfers, of e-notatie, want een 'e' is ook een hex-teken. Gemeten
over 200.000 hashes gebeurt dat 1 op ~1400 keer (102x int, 42x float). yq's ``env()`` raadt
dan een getal, schrijft de annotatie ongequote weg, en Kubernetes weigert het manifest:
``annotations`` is ``map[string]string``. De sync faalt met

    Deployment in version "v1" cannot be handled as a Deployment: json: cannot unmarshal
    number into Go struct field ObjectMeta.spec.template.metadata.annotations of type string

en blijft falen, want dezelfde secrets leveren dezelfde hash. Gezien op wies/pr-672 (hash
3048182736028951): de Deployments werden nooit aangemaakt, terwijl de Secrets, Services,
Ingress en VPA's van diezelfde Application gewoon syncten.

De test draait de yq-expressie uit de plugin zelf, zodat hij meeverhuist als die verandert.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ruamel.yaml import YAML
from tests.programma import echt_programma

_PLUGIN = Path(__file__).parent.parent.parent.parent / "bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml"

#: Een hash die als getal wordt gelezen zodra iemand de quotes laat vallen.
_ALL_DIGIT_HASH = "3048182736028951"

_DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pr-672-frontend
spec:
  template:
    metadata:
      labels:
        app: pr-672-frontend
"""


def _injection_expression() -> str:
    """De yq-expressie waarmee de plugin de annotatie stempelt, uit de plugin zelf."""
    script = _PLUGIN.read_text()
    match = re.search(r"yq eval-all '\n(\s*with\(select\(\.kind == \"Deployment\".*?)'", script, re.DOTALL)
    assert match, "de injectie-expressie staat niet meer waar de test hem zoekt"
    return match.group(1)


def test_the_plugin_stamps_the_hash_as_a_string() -> None:
    """Zonder ``strenv`` komt hier ``checksum/config: 3048182736028951`` uit, en dat is een int."""
    yq = echt_programma("yq")

    result = subprocess.run(
        [yq, "eval-all", _injection_expression(), "-"],
        input=_DEPLOYMENT,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "CONFIG_HASH": _ALL_DIGIT_HASH},
    )

    doc = YAML().load(result.stdout)
    stamped = doc["spec"]["template"]["metadata"]["annotations"]["checksum/config"]

    assert isinstance(stamped, str), f"Kubernetes weigert dit manifest: {stamped!r} is geen string"
    assert stamped == _ALL_DIGIT_HASH
