"""Wachten op een nieuwe Application mag niet alleen op ArgoCD's woord afgaan.

De ArgoCD-API geeft 403 voor zowel een applicatie die niet bestaat als voor een die hij
niet mag of kan zien, en ``application_exists`` leest dat als "bestaat niet".
``wait_for_application_deletion`` vangt die dubbelzinnigheid al af door de
Kubernetes-API als grondwaarheid te raadplegen; de aanmaakweg kreeg die controle nooit.

Wat dat kostte: toen ArgoCD de user-applications-repo niet kon lezen, bleef deze lus zes
minuten pollen en eindigde met "timed out waiting for application to be created", terwijl
de echte oorzaak een autorisatiefout was en de Application er niet toe deed.

Rood (zonder fallback): de eerste test loopt tot de timeout in plaats van meteen te slagen.
Groen: een harde True van de Kubernetes-API beeindigt het wachten meteen; False en None
laten het wachten doorlopen, net als aan de verwijderkant.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.manager.argo_manager import ArgoManager


@pytest.fixture
def argo_manager() -> ArgoManager:
    return ArgoManager(MagicMock())


def _argo_zegt_afwezig() -> AsyncMock:
    connector = AsyncMock()
    connector.login = AsyncMock(return_value=True)
    connector.application_exists = AsyncMock(return_value=False)
    return connector


@pytest.mark.asyncio
async def test_kubernetes_weet_het_beter_en_beeindigt_het_wachten(argo_manager: ArgoManager) -> None:
    argo = _argo_zegt_afwezig()
    kubectl = AsyncMock()
    kubectl.argocd_application_exists = AsyncMock(return_value=True)

    with (
        patch("opi.connectors.argo.ArgoConnector", return_value=argo),
        patch("opi.connectors.kubectl.create_kubectl_connector", return_value=kubectl),
        patch("asyncio.sleep", new_callable=AsyncMock) as slaap,
    ):
        resultaat = await argo_manager.wait_for_application_created("test-productie", timeout=60)

    assert resultaat is True
    kubectl.argocd_application_exists.assert_awaited_with("test-productie")
    # Geen enkele wachtronde: de tweede mening kwam op de eerste poging binnen.
    slaap.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("k8s_antwoord", [False, None])
async def test_onbevestigd_blijft_wachten(argo_manager: ArgoManager, k8s_antwoord: bool | None) -> None:
    """Alleen een harde True telt. Afwezig en onbekend zijn allebei geen reden om te stoppen."""
    argo = _argo_zegt_afwezig()
    kubectl = AsyncMock()
    kubectl.argocd_application_exists = AsyncMock(return_value=k8s_antwoord)

    with (
        patch("opi.connectors.argo.ArgoConnector", return_value=argo),
        patch("opi.connectors.kubectl.create_kubectl_connector", return_value=kubectl),
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(TimeoutError),
    ):
        await argo_manager.wait_for_application_created("test-productie", timeout=10)
