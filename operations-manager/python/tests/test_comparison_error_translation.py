"""Een ComparisonError moet zeggen wat er echt misging.

ArgoCD gebruikt dezelfde conditie voor "ik kon de repository niet ophalen" en "ik kon de
manifesten niet renderen". De portal legde dat altijd uit als het tweede ("vaak staan er
twee resources met dezelfde naam"), dus wie een fetch-timeout kreeg ging zijn manifesten
uitpluizen terwijl daar niets mis mee was.
"""

from opi.services.event_interpreter import interpret_argocd_errors

# Letterlijk de melding van amtbz-2m9/productie op 2026-08-18.
FETCH_TIMEOUT = (
    "failed to load target state: failed to evaluate revision changes for source 1 of 1: "
    "failed to update revision for paths: rpc error: code = Internal desc = unable to "
    "checkout git repo https://amtbz-2m9@github.com/RijksICTGilde/rig-cluster-application-test.git "
    "with revision 2a6006c34b4e1caf644baf034da8e2efd8efab39: failed to initialize repository "
    "resources: rpc error: code = Internal desc = Failed to fetch default: "
    "`git fetch origin --tags --force --prune` failed timeout after 1m30s"
)

RENDER_FOUT = (
    "Failed to load target state: failed to generate manifests in 'x': exit status 1: "
    "may not add resource with an already registered id: PersistentVolumeClaim.v1.[noGrp]/web-data.ns"
)

HELM_DEPENDENCY = (
    "Failed to load target state: failed to generate manifests in 'x': exit status 1: "
    "Error: failed to fetch https://charts.example.org/redis-1.2.3.tgz : 404 Not Found"
)


def _vertaal(raw: str) -> dict[str, str]:
    result = interpret_argocd_errors([{"resource": "ComparisonError", "message": raw}])
    assert len(result) == 1
    return result[0]


def test_fetch_timeout_wijst_naar_de_repository_niet_naar_de_manifesten() -> None:
    error = _vertaal(FETCH_TIMEOUT)
    assert error["resource"] == "Repository niet bereikbaar"
    assert "Git-repository niet ophalen" in error["suggestion"]
    assert "dezelfde naam" not in error["suggestion"]
    # Niets voor de gebruiker om op te lossen, dus geen actiepunt.
    assert error["severity"] == "informational"


def test_echte_renderfout_houdt_de_oude_uitleg() -> None:
    error = _vertaal(RENDER_FOUT)
    assert error["resource"] == "Configuratiefout (kustomize CMP)"
    assert "dezelfde naam" in error["suggestion"]
    assert error["severity"] == "actionable"


def test_helm_dependency_is_een_renderfout_geen_repositoryfout() -> None:
    """'failed to fetch' alleen is niet genoeg: Helm zegt dat over een chart."""
    error = _vertaal(HELM_DEPENDENCY)
    assert error["resource"] == "Configuratiefout (kustomize CMP)"


def test_ontbrekend_pad_krijgt_eigen_uitleg() -> None:
    error = _vertaal("failed to load target state: app path does not exist")
    assert error["resource"] == "Pad bestaat niet in de repository"
    assert error["severity"] == "actionable"


def test_verlopen_toegang_krijgt_eigen_uitleg() -> None:
    error = _vertaal("failed to load target state: authentication required for repo")
    assert error["resource"] == "Geen toegang tot de repository"


def test_onbekende_revisie_krijgt_eigen_uitleg() -> None:
    error = _vertaal("failed to load target state: unknown revision 'refs/heads/nope'")
    assert error["resource"] == "Revisie niet gevonden"
