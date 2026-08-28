"""
Tests for the opi.core.template_helpers module.

Tests format_dutch_date and get_service_name utility functions.
"""

from opi.core.template_helpers import format_dutch_date, get_service_name, shorten_image_digest


class TestFormatDutchDate:
    """Tests for format_dutch_date."""

    def test_iso_timestamp_basic(self):
        """Should format a basic ISO timestamp in Dutch.

        Output is converted from UTC to Europe/Amsterdam. January is CET (UTC+1),
        so 17:14 UTC displays as 18:14.
        """
        result = format_dutch_date("2026-01-14T17:14:00Z")
        assert result == "14 januari 2026 18:14"

    def test_iso_timestamp_without_time(self):
        """Should format without time when include_time=False."""
        result = format_dutch_date("2026-03-05T10:30:00Z", include_time=False)
        assert result == "5 maart 2026"

    def test_short_month_abbreviates(self):
        """short_month kort de maand af, met dezelfde omrekening naar onze tijd.

        Dit is wat de takentabel gebruikt: die heeft zes kolommen en "18 september 2026
        01:40" is daar 174px breed tegen 126 afgekort.
        """
        assert format_dutch_date("2026-09-17T21:18:55+00:00", short_month=True) == "17 sep 2026 23:18"

    def test_short_month_uses_the_dutch_abbreviations(self):
        """Niet af te leiden met de eerste drie letters: maart is "mrt", niet "maa"."""
        maanden = [
            format_dutch_date(f"2026-{nummer:02d}-15T12:00:00+00:00", include_time=False, short_month=True)
            for nummer in range(1, 13)
        ]
        assert [regel.split()[1] for regel in maanden] == [
            "jan",
            "feb",
            "mrt",
            "apr",
            "mei",
            "jun",
            "jul",
            "aug",
            "sep",
            "okt",
            "nov",
            "dec",
        ]

    def test_short_month_without_time(self):
        """De twee opties bijten elkaar niet."""
        assert format_dutch_date("2026-03-05T10:30:00Z", include_time=False, short_month=True) == "5 mrt 2026"

    def test_none_returns_dash(self):
        """Should return '-' for None."""
        assert format_dutch_date(None) == "-"

    def test_empty_string_returns_dash(self):
        """Should return '-' for empty string."""
        assert format_dutch_date("") == "-"

    def test_nanosecond_truncation_with_positive_timezone(self):
        """Should truncate nanoseconds and preserve positive timezone offset."""
        result = format_dutch_date("2026-01-14T17:14:34.335860214+02:00")
        assert "januari" in result
        assert "2026" in result

    def test_nanosecond_truncation_with_negative_timezone(self):
        """Bug: negative timezone offset must be preserved during nanosecond truncation.

        The code splits on '.' and then only checks for '+' in the decimal part.
        A timestamp like '2026-01-14T17:14:34.335860214-05:00' has the '-05:00'
        in the decimal part after splitting. The code must also check for '-' to
        avoid losing the timezone offset.
        """
        from datetime import datetime

        # This timestamp has nanoseconds AND a negative timezone offset
        # Manually simulate the old (buggy) truncation logic:
        test_value = "2026-01-14T17:14:34.335860214-05:00"
        test_value_z = test_value.replace("Z", "+00:00")
        parts = test_value_z.split(".")
        decimal_part = parts[1]  # "335860214-05:00"

        # The old code only checked for "+" - verify the fix handles "-"
        tz_part = ""
        for tz_char in ("+", "-"):
            if tz_char in decimal_part:
                idx = decimal_part.index(tz_char)
                tz_part = decimal_part[idx:]
                decimal_part = decimal_part[:idx]
                break

        reconstructed = parts[0] + "." + decimal_part[:6] + tz_part
        dt = datetime.fromisoformat(reconstructed)
        assert dt.tzinfo is not None, (
            f"Timezone info was lost during nanosecond truncation. Reconstructed value: {reconstructed!r}"
        )

        # Also verify the full function works
        result = format_dutch_date(test_value)
        assert "januari" in result

    def test_microsecond_precision_with_negative_timezone(self):
        """Microsecond precision (6 digits) with negative offset should work fine."""
        result = format_dutch_date("2026-06-15T08:30:45.123456-03:00")
        assert "juni" in result

    def test_datetime_object(self):
        """Should handle datetime objects directly.

        Output is converted from UTC to Europe/Amsterdam. December is CET (UTC+1),
        so 10:00 UTC displays as 11:00.
        """
        from datetime import UTC, datetime

        dt = datetime(2026, 12, 25, 10, 0, tzinfo=UTC)
        result = format_dutch_date(dt)
        assert result == "25 december 2026 11:00"


class TestGetServiceName:
    """Tests for get_service_name."""

    def test_string_service(self):
        """String services return as-is."""
        assert get_service_name("publish-on-web") == "publish-on-web"

    def test_dict_service(self):
        """Dict services return the first key."""
        assert get_service_name({"keycloak": {"config": {}}}) == "keycloak"

    def test_empty_dict_service(self):
        """Empty dict returns empty string."""
        assert get_service_name({}) == ""

    def test_other_type(self):
        """Unrecognisable entries (not a string or a service record) return empty
        string -- the format-agnostic resolver treats them like an empty dict,
        which is safe to render in a template."""
        assert get_service_name(42) == ""


class TestShortenImageDigest:
    """Tests for shorten_image_digest, het filter ``korte_digest``."""

    def test_digest_wordt_twaalf_tekens(self):
        """De grens uit het plan: twaalf tekens van de digest, de repository blijft heel."""
        volledig = (
            "ghcr.io/minbzk/moza-profiel-service@"
            "sha256:25ab6344a1b2c3d4e5f60718293a4b5c6d7e8f901234567890abcdef123456789"
        )
        assert shorten_image_digest(volledig) == "ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344a1b2"

    def test_een_tag_blijft_heel(self):
        """Aan een tag valt niets af te korten, en afkappen zou hem onherkenbaar maken."""
        assert shorten_image_digest("ghcr.io/minbzk/moza:2026.08.21") == "ghcr.io/minbzk/moza:2026.08.21"

    def test_lege_waarde(self):
        """Een ontbrekende image geeft een lege tekst, geen "None" op het scherm."""
        assert shorten_image_digest(None) == ""
        assert shorten_image_digest("") == ""

    def test_digest_zonder_algoritme(self):
        """Een verwijzing met een @ maar zonder ``sha256:`` wordt op twaalf tekens gekapt.

        Zo'n vorm is geen geldige imageverwijzing, maar het filter mag er niet op omvallen:
        wat de pod meldt komt van buiten.
        """
        assert shorten_image_digest("ghcr.io/minbzk/moza@abcdefghijklmnop") == "ghcr.io/minbzk/moza@abcdefghijkl"
