"""Regression test for the authorization-wall YAML injection (Vuln 2).

The authorization-wall sidecar template renders several user-derived project
values into Kubernetes manifests. They were emitted as unquoted / naively
double-quoted YAML scalars with Jinja2 autoescape=False, so a value containing
a newline (and, for the quoted ones, a `"`) plus crafted indentation could
break out of its scalar and inject sibling YAML keys. The generated manifests
are committed and applied by ArgoCD, so this is privilege escalation:

  - container `args:` -> inject a privileged securityContext on the sidecar
  - ConfigMap `metadata` -> attacker-chosen resource name/namespace, or
    arbitrary sibling keys in another tenant's namespace

The fix renders every user-derived scalar via `| tojson` (a JSON string is a
valid YAML flow scalar; tojson escapes quotes and newlines).

Red (vulnerable template): the parsed manifest gains attacker keys, so the
assertions fail.
Green (fixed template): every payload stays a single literal scalar.
"""

from opi.generation.manifests import render_template
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

# A value that, rendered unquoted/quoted in a YAML scalar, tries to close the
# scalar and inject sibling keys with matching indentation.
INJECTION_BANNER = (
    "pwn\n"
    "          securityContext:\n"
    "            privileged: true\n"
    "            runAsUser: 0\n"
    "            allowPrivilegeEscalation: true"
)

# Breaks out of a double-quoted scalar (leading `"`) then injects siblings.
INJECTION_NAME = 'evil"\n  injectedKey: injected\n  notAName: "pwned'
INJECTION_NAMESPACE = 'kube-system"\n  injectedNs: injected\n  x: "y'
INJECTION_PROJECT = 'proj"\n  injectedProject: injected\n  z: "w'

# Same class of payload for the OIDC args that the fix also touched.
INJECTION_ISSUER = "https://evil/\n            injectedArg: true\n          x: y"
INJECTION_CLIENT = "cid\n          injectedClient: true"
INJECTION_HOSTNAME = "h\n          injectedHost: true"


def _render_container(**overrides) -> str:
    aw = {
        "issuer_url": "https://keycloak.example.com/realms/r",
        "client_id": "myapp",
        "banner": INJECTION_BANNER,
        "keycloak_secret_name": "myapp-oidc",
        "cookie_secret_name": "myapp-cookie",
    }
    aw.update(overrides.pop("authorization_wall", {}))
    ctx = {
        "section": "container",
        "application_port": 8080,
        "hostname": "app.example.com",
        "name": "myapp",
        "authorization_wall": aw,
    }
    ctx.update(overrides)
    return render_template("sidecar-authorization-wall.yaml.jinja", ctx)


def _container(rendered: str) -> dict:
    # The template emits a list item under an 8-space indent; wrap so it parses.
    doc = YAML().load("containers:\n" + rendered)
    containers = doc["containers"]
    assert len(containers) == 1, f"injection altered container count: {containers}"
    c = containers[0]
    assert c["name"] == "authorization-wall"
    return c


def _assert_sidecar_not_escalated(container: dict) -> None:
    sec = container.get("securityContext", {})
    assert sec.get("privileged") is not True, "injected privileged securityContext"
    assert sec.get("runAsUser") != 0, "injected runAsUser: 0"
    assert sec.get("allowPrivilegeEscalation") is not True, "injected allowPrivilegeEscalation"
    assert sec.get("runAsNonRoot") is True, "hardened securityContext was overwritten"
    # No attacker key may have appeared at the container-mapping level.
    for forbidden in ("injectedArg", "injectedClient", "injectedHost", "injectedKey"):
        assert forbidden not in container, f"injected sibling key {forbidden!r} into container"


def test_banner_cannot_inject_security_context() -> None:
    container = _container(_render_container())
    _assert_sidecar_not_escalated(container)
    banner_args = [a for a in container["args"] if str(a).startswith("--banner=")]
    assert len(banner_args) == 1
    assert banner_args[0] == f"--banner={INJECTION_BANNER}"


def test_oidc_args_cannot_inject() -> None:
    """issuer_url / client_id / hostname were also changed by the fix."""
    container = _container(
        _render_container(
            hostname=INJECTION_HOSTNAME,
            authorization_wall={
                "issuer_url": INJECTION_ISSUER,
                "client_id": INJECTION_CLIENT,
                "banner": "",
            },
        )
    )
    _assert_sidecar_not_escalated(container)
    args = container["args"]
    assert f"--oidc-issuer-url={INJECTION_ISSUER}" in args
    assert f"--client-id={INJECTION_CLIENT}" in args
    assert f"--redirect-url=https://{INJECTION_HOSTNAME}/oauth2/callback" in args


def test_configmap_metadata_cannot_inject() -> None:
    """The configmap section: name/namespace/project.name were injectable."""
    rendered = render_template(
        "sidecar-authorization-wall.yaml.jinja",
        {
            "section": "configmap",
            "name": INJECTION_NAME,
            "namespace": INJECTION_NAMESPACE,
            "project": {"name": INJECTION_PROJECT},
        },
    )
    docs = list(YAML().load_all(rendered))
    assert len(docs) == 1, "configmap injection produced extra YAML documents"
    cm = docs[0]
    assert cm["kind"] == "ConfigMap"
    md = cm["metadata"]
    # Names/namespace must be exactly the literal (escaped) payload, no siblings.
    assert md["name"] == f"{INJECTION_NAME}-oauth2-signin"
    assert md["namespace"] == INJECTION_NAMESPACE
    assert md["labels"]["app"] == INJECTION_NAME
    assert md["labels"]["project"] == INJECTION_PROJECT
    for forbidden in ("injectedKey", "injectedNs", "injectedProject", "notAName"):
        assert forbidden not in md, f"injected sibling key {forbidden!r} into ConfigMap metadata"
    assert "injectedNs" not in cm, "injected sibling key into ConfigMap top level"
    assert "injectedProject" not in cm, "injected sibling key into ConfigMap top level"


def test_volumes_section_cannot_inject() -> None:
    rendered = render_template(
        "sidecar-authorization-wall.yaml.jinja",
        {"section": "volumes", "name": INJECTION_NAME},
    )
    vols = YAML().load("volumes:\n" + rendered)["volumes"]
    assert len(vols) == 1, f"volumes injection altered volume count: {vols}"
    assert vols[0]["configMap"]["name"] == f"{INJECTION_NAME}-oauth2-signin"
    assert "injectedKey" not in vols[0]


CONFIGMAP_BASIS = {"section": "configmap", "name": "app", "namespace": "ns", "project": {"name": "p"}}


def _configmap_documenten(veld: str, ctx: dict) -> list:
    """Parse de gerenderde configmap, en noem het veld als de payload de YAML brak."""
    gerenderd = render_template("sidecar-authorization-wall.yaml.jinja", ctx)
    try:
        return list(YAML().load_all(gerenderd))
    except YAMLError as e:
        raise AssertionError(f"{veld}: de payload brak de configmap uit zijn scalar: {e}") from e


def test_de_templates_blijven_heel_bij_een_injectiepoging() -> None:
    """De configmap draagt sinds RC-244 twee templates in een block scalar.

    Die blokken bevatten zelf geen Jinja-interpolatie, maar ze staan onder dezelfde
    `data:` als de geescapete metadata erboven. Breekt een payload daar uit zijn scalar,
    dan verschuift of verdwijnt `error.html` en valt oauth2-proxy terug op zijn Engelse
    ingebouwde pagina, zonder dat er iets rood wordt.
    """
    schoon = YAML().load(render_template("sidecar-authorization-wall.yaml.jinja", CONFIGMAP_BASIS))
    verwacht = schoon["data"]["error.html"]

    # Een payload die het `data`-blok probeert te overschrijven in plaats van metadata.
    kaper = 'pwn\n  data:\n    error.html: "GEKAAPT"\n  x: "y'
    # De vier waarden die de configmap-sectie interpoleert. `banner` staat hier niet bij:
    # die wordt alleen in de container-sectie gerenderd.
    for veld, ctx in (
        ("name", {"name": kaper}),
        ("namespace", {"namespace": kaper}),
        ("project.name", {"project": {"name": kaper}}),
        ("extra_labels-waarde", {"extra_labels": {"gekaapt": kaper}}),
    ):
        docs = _configmap_documenten(veld, CONFIGMAP_BASIS | ctx)
        assert len(docs) == 1, f"{veld}: injectie leverde extra YAML-documenten op"
        data = docs[0]["data"]
        assert sorted(data) == ["error.html", "sign_in.html"], f"{veld}: data-sleutels gewijzigd"
        assert data["error.html"] == verwacht, f"{veld}: error.html is gewijzigd door de payload"
