"""De tweede factor van een ZAD-gebruiker, tegen een echte Postgres (RC-329).

Elke methode van :class:`UserOtpService` staat hier, plus de gevallen die het plan
noemt: geen factor geeft "niet geregistreerd" en geen fout, de vijfde fout blokkeert, en
tijdens de blokkade faalt ook een goede code. De weg terug is de reset door een
beheerder; herstelcodes bestaan niet.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from opi.services.user_admin_service import UserAdminService
from opi.services.user_otp_service import OTP_MAX_FAILED_ATTEMPTS, OtpVerification, UserOtpService
from opi.utils.totp import totp_now

EMAIL = "otp@example.nl"


@pytest.fixture
def otp_keys(monkeypatch: pytest.MonkeyPatch, age_keypair: tuple[str, str]) -> None:
    """De platformsleutel waarmee ``otp_secret`` versleuteld wordt."""
    private_key, public_key = age_keypair
    monkeypatch.setattr("opi.core.config.settings.SOPS_AGE_PUBLIC_KEY", public_key)
    monkeypatch.setattr("opi.core.config.settings.SOPS_AGE_PRIVATE_KEY", private_key)


async def _registreer(svc: UserOtpService, email: str = EMAIL) -> None:
    """Een bevestigde factor."""
    registratie = await svc.start_registration(email)
    code, _ = totp_now(registratie.secret)
    assert await svc.confirm_registration(email, code, registratie.encrypted_secret, full_name="Otto") is True


# --- registreren ---------------------------------------------------------------------


async def test_zonder_registratie_is_er_geen_factor(orm_db, otp_keys) -> None:
    svc = UserOtpService()
    status = await svc.status("onbekend@example.nl")
    assert status.registered is False
    assert status.confirmed_at is None


async def test_registreren_levert_een_qr_en_een_zaad_dat_niet_in_de_database_staat(orm_db, otp_keys) -> None:
    svc = UserOtpService()
    registratie = await svc.start_registration(EMAIL)

    assert registratie.otpauth_uri.startswith("otpauth://totp/")
    assert registratie.base32_secret
    assert registratie.secret not in registratie.encrypted_secret
    # Start schrijft nog niets: slechts een geldige code maakt de factor echt.
    assert (await svc.status(EMAIL)).registered is False


async def test_bevestigen_slaat_de_factor_op(orm_db, otp_keys) -> None:
    svc = UserOtpService()
    await _registreer(svc)

    status = await svc.status(EMAIL)
    assert status.registered is True
    assert status.confirmed_at is not None


async def test_een_verkeerde_bevestigingscode_slaat_niets_op(orm_db, otp_keys) -> None:
    svc = UserOtpService()
    registratie = await svc.start_registration(EMAIL)
    goed, _ = totp_now(registratie.secret)
    fout = "000000" if goed != "000000" else "111111"

    assert await svc.confirm_registration(EMAIL, fout, registratie.encrypted_secret) is False
    assert (await svc.status(EMAIL)).registered is False


async def test_resume_toont_dezelfde_registratie_na_een_verkeerde_code(orm_db, otp_keys) -> None:
    """``otp_bevestigen`` roept dit aan om de QR opnieuw te tonen na een foute code.

    Zonder deze test loopt geen enkele test door ``resume_registration`` heen: de
    routetests mocken de service en de e2e-stub heeft zijn eigen implementatie.
    """
    svc = UserOtpService()
    registratie = await svc.start_registration(EMAIL)

    hervat = await svc.resume_registration(EMAIL, registratie.encrypted_secret)

    assert hervat.secret == registratie.secret
    assert hervat.encrypted_secret == registratie.encrypted_secret
    assert hervat.otpauth_uri == registratie.otpauth_uri
    assert hervat.base32_secret == registratie.base32_secret
    # Dezelfde code als van het oorspronkelijke zaad is nu ook geldig voor bevestiging.
    code, _ = totp_now(hervat.secret)
    assert await svc.confirm_registration(EMAIL, code, hervat.encrypted_secret) is True


async def test_het_zaad_staat_versleuteld_in_de_kolom(orm_db, otp_keys) -> None:
    from opi.core.db import session_scope
    from opi.services.persistence.users import User
    from sqlalchemy import select

    svc = UserOtpService()
    registratie = await svc.start_registration(EMAIL)
    code, _ = totp_now(registratie.secret)
    await svc.confirm_registration(EMAIL, code, registratie.encrypted_secret)

    async with session_scope() as session:
        row = (await session.execute(select(User).where(User.email == EMAIL))).scalar_one()
        assert row.otp_secret is not None
        assert registratie.secret not in row.otp_secret
        assert "BEGIN AGE ENCRYPTED FILE" in row.otp_secret
        assert row.otp_key_version == 1
        # to_dict() is wat de beheerpagina's lezen, en draagt geen OTP-veld.
        assert not [k for k in row.to_dict() if k.startswith("otp")]


async def test_opnieuw_registreren_vervangt_het_zaad(orm_db, otp_keys) -> None:
    from opi.core.db import session_scope
    from opi.services.persistence.users import User
    from sqlalchemy import select

    svc = UserOtpService()
    eerste = await svc.start_registration(EMAIL)
    code, _ = totp_now(eerste.secret)
    await svc.confirm_registration(EMAIL, code, eerste.encrypted_secret)

    tweede = await svc.start_registration(EMAIL)
    # De oude factor blijft tot de nieuwe bevestigd is.
    async with session_scope() as session:
        row = (await session.execute(select(User).where(User.email == EMAIL))).scalar_one()
        assert row.otp_secret == eerste.encrypted_secret

    code, _ = totp_now(tweede.secret)
    assert await svc.confirm_registration(EMAIL, code, tweede.encrypted_secret) is True

    # Alleen de nieuwe app levert nog een geldige code.
    volgende, _ = totp_now(tweede.secret, at=datetime.now(UTC).timestamp() + 30)
    assert await svc.verify(EMAIL, volgende) == OtpVerification.OK


async def test_registreren_maakt_de_gebruikersrij_aan_als_die_er_niet_is(orm_db, otp_keys) -> None:
    svc = UserOtpService()
    await _registreer(svc)
    rij = await UserAdminService().get_user_by_email(EMAIL)
    assert rij is not None
    assert rij["full_name"] == "Otto"


async def test_registreren_gebruikt_een_bestaande_gebruikersrij(orm_db, otp_keys) -> None:
    bestaand = await UserAdminService().create_user(EMAIL, "Otto Bestaand")
    svc = UserOtpService()
    await _registreer(svc)

    rij = await UserAdminService().get_user(bestaand["id"])
    assert rij is not None
    assert rij["full_name"] == "Otto Bestaand"


async def test_een_hoofdletter_in_het_adres_levert_geen_tweede_rij_op(orm_db, otp_keys) -> None:
    """Een beheerder typt het adres zoals hij het kent, de inlogdienst levert het klein."""
    admin = UserAdminService()
    bestaand = await admin.create_user("Otto.Groot@example.nl", "Otto Groot")
    svc = UserOtpService()
    await _registreer(svc, "otto.groot@example.nl")

    assert len(await admin.list_users()) == 1
    assert (await svc.status("otto.groot@example.nl")).registered is True
    # De resetknop op de rij van de beheerder raakt dezelfde factor.
    assert await svc.reset(bestaand["email"], by="beheerder@example.nl") is True
    assert (await svc.status("otto.groot@example.nl")).registered is False


# --- verifieren ----------------------------------------------------------------------


async def test_verify_zonder_factor_is_niet_geregistreerd_en_geen_fout(orm_db, otp_keys) -> None:
    assert await UserOtpService().verify("onbekend@example.nl", "123456") == OtpVerification.NOT_REGISTERED


async def test_verify_accepteert_een_code_en_weigert_hem_daarna(orm_db, otp_keys) -> None:
    svc = UserOtpService()
    registratie = await svc.start_registration(EMAIL)
    code, _ = totp_now(registratie.secret)
    await svc.confirm_registration(EMAIL, code, registratie.encrypted_secret)

    # Dezelfde stap is bij de bevestiging al verbruikt.
    assert await svc.verify(EMAIL, code) == OtpVerification.INVALID

    volgende, _ = totp_now(registratie.secret, at=datetime.now(UTC).timestamp() + 30)
    assert await svc.verify(EMAIL, volgende) == OtpVerification.OK
    assert await svc.verify(EMAIL, volgende) == OtpVerification.INVALID


async def test_de_vijfde_fout_blokkeert_en_dan_faalt_ook_een_goede_code(orm_db, otp_keys) -> None:
    svc = UserOtpService()
    registratie = await svc.start_registration(EMAIL)
    code, _ = totp_now(registratie.secret)
    await svc.confirm_registration(EMAIL, code, registratie.encrypted_secret)

    for _ in range(OTP_MAX_FAILED_ATTEMPTS):
        assert await svc.verify(EMAIL, "000000") == OtpVerification.INVALID

    status = await svc.status(EMAIL)
    assert status.locked is True
    assert status.locked_until is not None

    goed, _ = totp_now(registratie.secret, at=datetime.now(UTC).timestamp() + 30)
    assert await svc.verify(EMAIL, goed) == OtpVerification.LOCKED


async def test_een_geslaagde_poging_zet_de_teller_op_nul(orm_db, otp_keys) -> None:
    from opi.core.db import session_scope
    from opi.services.persistence.users import User
    from sqlalchemy import select

    svc = UserOtpService()
    registratie = await svc.start_registration(EMAIL)
    code, _ = totp_now(registratie.secret)
    await svc.confirm_registration(EMAIL, code, registratie.encrypted_secret)

    for _ in range(OTP_MAX_FAILED_ATTEMPTS - 1):
        assert await svc.verify(EMAIL, "000000") == OtpVerification.INVALID
    goed, _ = totp_now(registratie.secret, at=datetime.now(UTC).timestamp() + 30)
    assert await svc.verify(EMAIL, goed) == OtpVerification.OK

    async with session_scope() as session:
        row = (await session.execute(select(User).where(User.email == EMAIL))).scalar_one()
        assert row.otp_failed_attempts == 0
        assert row.otp_locked_until is None


# --- herstellen ----------------------------------------------------------------------


async def test_reset_maakt_de_factor_leeg(orm_db, otp_keys) -> None:
    from opi.core.db import session_scope
    from opi.services.persistence.users import User
    from opi.services.user_otp_service import OTP_COLUMNS_CLEARED_BY_RESET
    from sqlalchemy import select

    svc = UserOtpService()
    await _registreer(svc)
    # Ook een geblokkeerde otp_locked_until moet leeg worden: anders bewijst de
    # assertie hieronder niets voor die kolom, want hij begint al op None.
    for _ in range(OTP_MAX_FAILED_ATTEMPTS):
        await svc.verify(EMAIL, "000000")
    assert (await svc.status(EMAIL)).locked is True

    assert await svc.reset(EMAIL, by="beheerder@example.nl") is True
    assert (await svc.status(EMAIL)).registered is False
    assert await svc.verify(EMAIL, "000000") == OtpVerification.NOT_REGISTERED

    async with session_scope() as session:
        row = (await session.execute(select(User).where(User.email == EMAIL))).scalar_one()
        leeg = {"otp_key_version": 1, "otp_failed_attempts": 0}
        for kolom in OTP_COLUMNS_CLEARED_BY_RESET:
            assert getattr(row, kolom) == leeg.get(kolom), kolom
    # De gebruiker zelf blijft staan.
    assert await UserAdminService().get_user_by_email(EMAIL) is not None


async def test_reset_van_een_onbekende_gebruiker_doet_niets(orm_db, otp_keys) -> None:
    assert await UserOtpService().reset("onbekend@example.nl", by="beheerder@example.nl") is False


async def test_een_reset_is_de_weg_terug_na_een_blokkade(orm_db, otp_keys) -> None:
    """Zonder herstelcodes is een reset door een beheerder de enige uitweg.

    Hij heft de blokkade op en laat de gebruiker opnieuw registreren.
    """
    svc = UserOtpService()
    await _registreer(svc)
    for _ in range(OTP_MAX_FAILED_ATTEMPTS):
        await svc.verify(EMAIL, "000000")
    assert (await svc.status(EMAIL)).locked is True

    assert await svc.reset(EMAIL, by="beheerder@example.nl") is True

    status = await svc.status(EMAIL)
    assert status.registered is False
    assert status.locked is False

    nieuwe = await svc.start_registration(EMAIL)
    code, _ = totp_now(nieuwe.secret)
    assert await svc.confirm_registration(EMAIL, code, nieuwe.encrypted_secret) is True
    assert (await svc.status(EMAIL)).registered is True


# --- loggen --------------------------------------------------------------------------


async def test_geen_zaad_of_code_in_de_logregels(orm_db, otp_keys, caplog) -> None:
    """Nooit het geheim of de code in een regel."""
    svc = UserOtpService()
    with caplog.at_level(logging.INFO, logger="opi.services.user_otp_service"):
        registratie = await svc.start_registration(EMAIL)
        code, _ = totp_now(registratie.secret)
        assert await svc.confirm_registration(EMAIL, code, registratie.encrypted_secret) is True
        for _ in range(OTP_MAX_FAILED_ATTEMPTS):
            await svc.verify(EMAIL, "000000")
        await svc.reset(EMAIL, by="beheerder@example.nl")

    regels = "\n".join(r.getMessage() for r in caplog.records)
    assert regels
    for geheim in (registratie.secret, registratie.base32_secret, code):
        assert geheim not in regels
    niveaus = {r.levelname for r in caplog.records}
    assert "INFO" in niveaus
    assert "WARNING" in niveaus
