"""Een ArgoConnector bouwen doet geen netwerkverkeer.

WAT ER MISGING

``__init__`` logde synchroon in met ``requests.post`` (timeout 10 s, ongeveer 700 ms aan
bcrypt) zolang de procesbrede token-cache leeg was. Dat gebeurde op de event loop, bij elk
van de 22 aanroepers van ``create_argo_connector``, en zolang ArgoCD onbereikbaar was
kostte elke constructie de volle timeout. ``ensure_authenticated`` logt async in, met
lock, op het moment dat de token echt nodig is.
"""

from __future__ import annotations

import ssl
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from opi.connectors import argo as argo_module
from opi.connectors.argo import ArgoConnector, ArgoTLSConfigurationError

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _lege_token_cache():
    argo_module._token_cache.clear()
    yield
    argo_module._token_cache.clear()


def test_de_constructor_praat_niet_met_het_netwerk() -> None:
    """Geen requests, geen aiohttp: de constructor zet alleen velden."""
    with patch("aiohttp.ClientSession", side_effect=AssertionError("de constructor opende een sessie")):
        connector = ArgoConnector()

    assert connector.auth_token is None
    assert not hasattr(ArgoConnector, "_perform_login"), "de synchrone login hoort weg te zijn"


def test_requests_is_niet_meer_geimporteerd() -> None:
    """De synchrone HTTP-client had hier geen andere gebruiker."""
    assert not hasattr(argo_module, "requests")


@pytest.mark.asyncio
async def test_de_eerste_async_aanroep_logt_in() -> None:
    connector = ArgoConnector()

    async def _login(self: ArgoConnector) -> bool:
        self.auth_token = "token-1"
        self._store_token("token-1")
        return True

    with patch.object(ArgoConnector, "login", _login):
        assert await connector.ensure_authenticated() is True

    assert connector.auth_token == "token-1"


@pytest.mark.asyncio
async def test_een_tweede_connector_neemt_de_token_over_zonder_login() -> None:
    ArgoConnector()._store_token("token-1")

    tweede = ArgoConnector()
    with patch.object(ArgoConnector, "login", AsyncMock(side_effect=AssertionError("onnodige login"))):
        assert await tweede.ensure_authenticated() is True

    assert tweede.auth_token == "token-1"


def test_de_ssl_context_wordt_een_keer_gemaakt() -> None:
    """``ssl.create_default_context`` las de CA-bundel van schijf, per request."""
    argo_module._ssl_context_voor.cache_clear()

    with patch("ssl.create_default_context", wraps=ssl.create_default_context) as maak:
        eerste = ArgoConnector(verify_ssl=True)._ssl_context()
        tweede = ArgoConnector(verify_ssl=True)._ssl_context()

    assert eerste is tweede
    assert maak.call_count == 1


def test_een_andere_verify_stand_krijgt_een_eigen_context() -> None:
    assert ArgoConnector(verify_ssl=True)._ssl_context() is not ArgoConnector(verify_ssl=False)._ssl_context()


def _ca_bundel(tmp_path: Path) -> str:
    """Een zelfondertekend CA-certificaat op schijf, zoals de gemounte bundel in het cluster."""
    sleutel = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    naam = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    nu = datetime.now(UTC)
    certificaat = (
        x509.CertificateBuilder()
        .subject_name(naam)
        .issuer_name(naam)
        .public_key(sleutel.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(nu)
        .not_valid_after(nu + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(sleutel, hashes.SHA256())
    )
    pad = tmp_path / "ca.crt"
    pad.write_bytes(certificaat.public_bytes(serialization.Encoding.PEM))
    return str(pad)


def test_tls_met_bundel_controleert_het_certificaat(tmp_path: Path) -> None:
    context = ArgoConnector(use_tls=True, verify_ssl=True, ca_bundle=_ca_bundel(tmp_path))._ssl_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_tls_met_controle_zonder_bundel_weigert() -> None:
    """De CA van de ArgoCD-server zit niet in de systeemtrust, dus dit faalt toch."""
    with pytest.raises(ArgoTLSConfigurationError):
        ArgoConnector(use_tls=True, verify_ssl=True)


def test_een_ontbrekende_bundel_noemt_het_pad(tmp_path: Path) -> None:
    """De mount is optional, dus een cluster zonder ConfigMap levert geen bestand."""
    argo_module._ssl_context_voor.cache_clear()
    ontbreekt = str(tmp_path / "service-ca.crt")

    with pytest.raises(ArgoTLSConfigurationError, match=ontbreekt):
        ArgoConnector(use_tls=True, verify_ssl=True, ca_bundle=ontbreekt)._ssl_context()


def test_een_lege_bundel_noemt_het_pad(tmp_path: Path) -> None:
    """De ConfigMap bestaat, maar de service-CA heeft service-ca.crt nog niet gevuld."""
    argo_module._ssl_context_voor.cache_clear()
    leeg = tmp_path / "service-ca.crt"
    leeg.write_text("")

    with pytest.raises(ArgoTLSConfigurationError, match=str(leeg)):
        ArgoConnector(use_tls=True, verify_ssl=True, ca_bundle=str(leeg))._ssl_context()


def test_zonder_bundel_valt_de_connector_niet_terug_op_plain_http(tmp_path: Path) -> None:
    """De basis-URL blijft https: het protocol is een instelling en geen gok."""
    argo_module._ssl_context_voor.cache_clear()
    connector = ArgoConnector(use_tls=True, verify_ssl=True, ca_bundle=str(tmp_path / "service-ca.crt"))

    assert connector.base_url.startswith("https://")
    with pytest.raises(ArgoTLSConfigurationError):
        connector._ssl_context()
