"""De opslag-chokepoint mag versleutelde velden niet als wijziging aanzien.

AGE hercodeert niet-deterministisch: dezelfde platte tekst levert elke keer andere
ciphertext. In ``_reconcile_with_concurrent_write`` staat al een vangregel voor het geval
dat onze wijziging AL gecommit is -- de modal slaat op en geeft hetzelfde resultaat door
aan de deployment-taak, die het tegen de basis van vóór die opslag nog eens schrijft. Die
regel is ``current == data``, en juist die miste bij een versleuteld veld, want dan zijn
twee inhoudelijk identieke versies nooit byte-gelijk.

Gevolg, gemeten in de doorloop van RC-118: op elk project met een versleuteld veld gaf de
domeinwizard PERMANENT "is gewijzigd sinds je begon met bewerken", terwijl er niemand
anders was en het bestand aantoonbaar niet bewoog. Hetzelfde project zonder zo'n veld sloeg
gewoon op.

Deze tests bewaken de UITLIJNING, en vooral de grenzen ervan. Het gevaar van zo'n
gelijkstelling is dat hij te veel gelijk noemt en daarmee een echte wijziging van iemand
anders opslokt. Daarom staat hier niet alleen "gelijke inhoud wordt uitgelijnd", maar ook
elk geval waarin dat NIET mag gebeuren.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

AGE = "-----BEGIN AGE ENCRYPTED FILE-----\n{}\n-----END AGE ENCRYPTED FILE-----"


def _store():
    from opi.services.project_store import GitProjectStore

    return GitProjectStore()


def _met_sleutel(store, plaintexts: dict[str, str]):
    """Doet alsof de projectsleutel er is en ontcijfert volgens de meegegeven tabel."""

    async def nep_decrypt(value, private_key, field):  # noqa: ANN001
        return plaintexts.get(value)

    return (
        patch("opi.services.project_store.get_decoded_project_private_key", AsyncMock(return_value="AGE-SECRET-KEY-1")),
        patch.object(type(store), "_try_decrypt", staticmethod(nep_decrypt)),
    )


class TestGelijkeInhoudWordtUitgelijnd:
    @pytest.mark.asyncio
    async def test_zelfde_platte_tekst_neemt_de_bestaande_ciphertext_over(self):
        store = _store()
        ours, theirs = AGE.format("AAA"), AGE.format("BBB")
        data = {"components": [{"user-env-vars": ours}]}
        ref = {"components": [{"user-env-vars": theirs}]}
        p1, p2 = _met_sleutel(store, {ours: "GEHEIM=1", theirs: "GEHEIM=1"})
        with p1, p2:
            uit = await store._align_ciphertext(data, ref)
        assert uit["components"][0]["user-env-vars"] == theirs
        assert data["components"][0]["user-env-vars"] == ours, "de invoer mag niet gemuteerd worden"


class TestWatVooralNIETUitgelijndMagWorden:
    """De grenzen. Elk van deze gevallen moet een verschil BLIJVEN."""

    @pytest.mark.asyncio
    async def test_andere_platte_tekst_blijft_een_wijziging(self):
        store = _store()
        ours, theirs = AGE.format("AAA"), AGE.format("BBB")
        data = {"x": ours}
        p1, p2 = _met_sleutel(store, {ours: "GEHEIM=nieuw", theirs: "GEHEIM=oud"})
        with p1, p2:
            uit = await store._align_ciphertext(data, {"x": theirs})
        assert uit["x"] == ours, "een echt gewijzigd geheim mag niet worden gelijkgesteld"

    @pytest.mark.asyncio
    async def test_zonder_projectsleutel_verandert_er_niets(self):
        store = _store()
        ours, theirs = AGE.format("AAA"), AGE.format("BBB")
        data = {"x": ours}
        with patch("opi.services.project_store.get_decoded_project_private_key", AsyncMock(return_value=None)):
            uit = await store._align_ciphertext(data, {"x": theirs})
        assert uit["x"] == ours

    @pytest.mark.asyncio
    async def test_mislukte_ontcijfering_stelt_niets_gelijk(self):
        store = _store()
        ours, theirs = AGE.format("AAA"), AGE.format("BBB")
        data = {"x": ours}
        p1, p2 = _met_sleutel(store, {})  # alles geeft None terug
        with p1, p2:
            uit = await store._align_ciphertext(data, {"x": theirs})
        assert uit["x"] == ours

    @pytest.mark.asyncio
    async def test_gewone_tekst_wordt_nooit_aangeraakt(self):
        store = _store()
        data = {"display-name": "mijn project"}
        p1, p2 = _met_sleutel(store, {"mijn project": "x", "ander project": "x"})
        with p1, p2:
            uit = await store._align_ciphertext(data, {"display-name": "ander project"})
        assert uit["display-name"] == "mijn project", "alleen AGE-blokken doen mee"

    @pytest.mark.asyncio
    async def test_een_sleutel_die_de_ander_niet_heeft_blijft_staan(self):
        store = _store()
        ours = AGE.format("AAA")
        data = {"nieuw-veld": ours}
        p1, p2 = _met_sleutel(store, {ours: "GEHEIM=1"})
        with p1, p2:
            uit = await store._align_ciphertext(data, {})
        assert uit["nieuw-veld"] == ours

    @pytest.mark.asyncio
    async def test_een_langere_lijst_dan_de_referentie_loopt_niet_stuk(self):
        store = _store()
        a, b = AGE.format("AAA"), AGE.format("BBB")
        data = {"l": [a, b]}
        p1, p2 = _met_sleutel(store, {a: "zelfde", b: "zelfde"})
        with p1, p2:
            uit = await store._align_ciphertext(data, {"l": [b]})
        assert uit["l"][0] == b
        assert uit["l"][1] == b or uit["l"][1] == a
