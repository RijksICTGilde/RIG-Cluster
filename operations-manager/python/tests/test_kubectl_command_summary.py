"""_summarize_kubectl_command logs 'operation X on project Y' without argv values.

The kubectl DEBUG log lines must never contain flag values, resource names or
stdin -- only the subcommand, the resource kind and the target namespace
(rig-prd-<project>). This is what keeps secret-creating commands from leaking
secrets into the log (and what makes the lines readable: "what, for which
project").
"""

from opi.connectors.kubectl import _summarize_kubectl_command


def test_verb_resource_and_project() -> None:
    assert _summarize_kubectl_command(["get", "pods", "-n", "rig-prd-regel-k4c"]) == (
        "kubectl get pods (project rig-prd-regel-k4c)"
    )


def test_namespace_equals_form() -> None:
    assert _summarize_kubectl_command(["get", "pods", "--namespace=rig-prd-amt"]) == (
        "kubectl get pods (project rig-prd-amt)"
    )


def test_flag_led_command_has_no_resource_kind() -> None:
    # `apply -f -` -> the token after the verb is a flag, so no resource kind.
    assert _summarize_kubectl_command(["apply", "-f", "-", "-n", "rig-prd-x"]) == "kubectl apply (project rig-prd-x)"


def test_empty_and_bare() -> None:
    assert _summarize_kubectl_command([]) == "kubectl"
    assert _summarize_kubectl_command(["version"]) == "kubectl version"


def test_never_leaks_secret_value_or_name() -> None:
    """A secret-creating command must not surface the secret value or name."""
    args = [
        "create",
        "secret",
        "generic",
        "rig-prd-app-db-secret",  # resource name
        "--from-literal=password=s3cr3t-value",  # secret value
        "-n",
        "rig-prd-app",
    ]
    summary = _summarize_kubectl_command(args)
    # Shows the operation + project...
    assert summary == "kubectl create secret (project rig-prd-app)"
    # ...and leaks nothing sensitive.
    for leak in ("s3cr3t-value", "from-literal", "password", "rig-prd-app-db-secret", "generic"):
        assert leak not in summary
